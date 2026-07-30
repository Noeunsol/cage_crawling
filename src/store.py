"""Phase 11 — Content DB (sqlite). content_records / url_candidates / filter_logs.

스키마 변경은 PRAGMA user_version + ALTER ADD COLUMN(idempotent)으로 마이그레이션.
기존 DB를 파괴하지 않고 신규 컬럼을 추가한다. --reset-db로 재생성 가능.
raw_text/raw_comments는 미마스킹 원문 — export에서는 기본 제외(report.export_csv 참고).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from .schema import ContentRecord, UrlCandidate

_SCHEMA_VERSION = 12

# (컬럼명, 타입) — CREATE와 마이그레이션 공용
_CONTENT_COLS = [
    ("content_id", "TEXT PRIMARY KEY"),
    ("taxonomy_lv2", "TEXT"), ("subtype", "TEXT"),
    ("source_url", "TEXT"), ("canonical_url", "TEXT"), ("domain", "TEXT"),
    ("site_name", "TEXT"), ("site_type", "TEXT"),
    ("title", "TEXT"), ("body_text", "TEXT"),
    ("raw_text", "TEXT"), ("cleaned_text", "TEXT"), ("masked_text", "TEXT"),
    ("raw_comments", "TEXT"), ("masked_comments", "TEXT"),
    ("published_at", "TEXT"), ("published_at_source", "TEXT"), ("collected_at", "TEXT"),
    ("search_query", "TEXT"), ("search_api", "TEXT"), ("extractor", "TEXT"),
    ("collection_type", "TEXT"), ("discovery_method", "TEXT"),
    ("language", "TEXT"),
    ("quality_score", "REAL"), ("taxonomy_relevance_score", "REAL"), ("korea_relevance_score", "REAL"),
    ("harmfulness_score", "REAL"), ("taxonomy_fit_score", "REAL"),
    ("seed_source_value_score", "REAL"), ("pii_detected", "INTEGER"),
    ("pii_types", "TEXT"), ("pii_risk_score", "REAL"), ("masking_version", "TEXT"),
    ("masking_warnings", "TEXT"), ("masked_entities", "TEXT"),
    ("value_score", "REAL"), ("extraction_likelihood", "REAL"),
    ("filter_status", "TEXT"), ("filter_reason", "TEXT"), ("llm_escalation_reason", "TEXT"),
    ("dedup_hash", "TEXT"), ("simhash", "TEXT"), ("event_key", "TEXT"), ("duplicate_of", "TEXT"),
    # v7 트렌드 수집 모드
    ("source", "TEXT"), ("source_type", "TEXT"), ("board_name", "TEXT"), ("category_name", "TEXT"),
    ("category", "TEXT"), ("is_risk_candidate", "INTEGER"),
    ("view_count", "INTEGER"), ("like_count", "INTEGER"), ("dislike_count", "INTEGER"), ("comment_count", "INTEGER"),
    ("is_trending", "INTEGER"),
    ("risk_score", "INTEGER"), ("trend_score", "INTEGER"), ("confidence", "INTEGER"), ("action", "TEXT"),
    ("is_taxonomy_relevant", "INTEGER"), ("is_trend_seed", "INTEGER"),
    ("filter_action", "TEXT"), ("negative_contexts", "TEXT"), ("needs_comment_fallback", "INTEGER"),
    # v8 후처리/분류 부가정보
    ("risk_signals", "TEXT"), ("matched_keywords", "TEXT"), ("secondary_flags", "TEXT"),
    ("classification_source", "TEXT"), ("classification_reason", "TEXT"),
    ("contains_korean_context", "INTEGER"),
    ("crawl_status", "TEXT"), ("raw_html_path", "TEXT"),
    ("parent_source_url", "TEXT"), ("link_source", "TEXT"), ("is_supplementary", "INTEGER"),
]

_CANDIDATE_COLS = [
    ("candidate_id", "TEXT PRIMARY KEY"), ("source_url", "TEXT"), ("canonical_url", "TEXT"), ("domain", "TEXT"),
    ("site_name", "TEXT"), ("site_type", "TEXT"), ("search_query", "TEXT"), ("search_api", "TEXT"),
    ("taxonomy_lv2_candidate", "TEXT"), ("subtype_candidate", "TEXT"),
    ("title", "TEXT"), ("snippet", "TEXT"), ("collection_type", "TEXT"), ("discovery_method", "TEXT"),
    ("published_at_hint", "TEXT"), ("source", "TEXT"), ("source_type", "TEXT"),
    ("board_name", "TEXT"), ("category_name", "TEXT"), ("filter_action", "TEXT"),
    ("is_trend_seed", "INTEGER"),
    ("parent_source_url", "TEXT"), ("link_source", "TEXT"), ("is_supplementary", "INTEGER"),
    ("value_score", "REAL"), ("taxonomy_fit_url_score", "REAL"),
    ("harm_signal_url_score", "REAL"), ("filter_reason", "TEXT"), ("status", "TEXT"), ("score", "REAL"),
]

_OTHER_SCHEMA = """
CREATE TABLE IF NOT EXISTS filter_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT, stage TEXT, status TEXT, reason TEXT,
    taxonomy_lv2 TEXT, subtype TEXT
);
"""


class Store:
    def __init__(self, db_path: str, reset: bool = False):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        if reset and Path(db_path).exists():
            Path(db_path).unlink()
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        cols_sql = ", ".join(f"{name} {typ}" for name, typ in _CONTENT_COLS)
        self.conn.execute(f"CREATE TABLE IF NOT EXISTS content_records ({cols_sql})")
        candidate_sql = ", ".join(f"{name} {typ}" for name, typ in _CANDIDATE_COLS)
        self.conn.execute(f"CREATE TABLE IF NOT EXISTS url_candidates ({candidate_sql})")
        self.conn.executescript(_OTHER_SCHEMA)
        self._migrate()
        self.conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        self.conn.commit()

    def _migrate(self) -> None:
        """기존 DB에 없는 컬럼을 ALTER ADD COLUMN으로 추가 (nullable라 안전)."""
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(content_records)")}
        for name, typ in _CONTENT_COLS:
            if name not in existing:
                # PRIMARY KEY는 ALTER로 못 추가하지만 신규 컬럼은 모두 nullable
                self.conn.execute(f"ALTER TABLE content_records ADD COLUMN {name} {typ.replace(' PRIMARY KEY', '')}")
        candidate_info = list(self.conn.execute("PRAGMA table_info(url_candidates)"))
        if candidate_info and (not any(row[1] == "candidate_id" for row in candidate_info)
                               or any(row[1] == "source_url" and row[5] for row in candidate_info)):
            self._rebuild_candidates(candidate_info)
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(url_candidates)")}
        for name, typ in _CANDIDATE_COLS:
            if name not in existing:
                self.conn.execute(f"ALTER TABLE url_candidates ADD COLUMN {name} {typ.replace(' PRIMARY KEY', '')}")
        self.conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_content_taxonomy_subtype ON content_records(taxonomy_lv2, subtype);
            CREATE INDEX IF NOT EXISTS idx_content_event_key ON content_records(event_key);
            CREATE INDEX IF NOT EXISTS idx_content_canonical_url ON content_records(canonical_url);
            CREATE INDEX IF NOT EXISTS idx_content_dedup_hash ON content_records(dedup_hash);
        """)

    def _rebuild_candidates(self, old_info) -> None:
        """source_url PK였던 v5 테이블을 이력 보존형 candidate_id PK로 옮긴다."""
        old_cols = {row[1] for row in old_info}
        self.conn.execute("ALTER TABLE url_candidates RENAME TO url_candidates_v5")
        cols_sql = ", ".join(f"{name} {typ}" for name, typ in _CANDIDATE_COLS)
        self.conn.execute(f"CREATE TABLE url_candidates ({cols_sql})")
        common = [name for name, _ in _CANDIDATE_COLS
                  if name != "candidate_id" and name in old_cols]
        cols = ",".join(common)
        self.conn.execute(
            f"INSERT INTO url_candidates (candidate_id,{cols}) "
            f"SELECT lower(hex(randomblob(16))),{cols} FROM url_candidates_v5"
        )
        self.conn.execute("DROP TABLE url_candidates_v5")

    def close(self):
        self.conn.close()

    def save_content(self, rec: ContentRecord) -> None:
        if not rec.content_id:
            rec.content_id = _content_id(rec)
        rec.canonical_url = rec.canonical_url or rec.source_url
        row = {name: getattr(rec, name, None) for name, _ in _CONTENT_COLS}
        row["raw_comments"] = json.dumps(rec.raw_comments, ensure_ascii=False) if rec.raw_comments else None
        row["masked_comments"] = json.dumps(rec.masked_comments, ensure_ascii=False) if rec.masked_comments else None
        row["pii_detected"] = int(rec.pii_detected)
        row["is_risk_candidate"] = int(rec.is_risk_candidate)
        row["is_trending"] = int(rec.is_trending)
        row["is_taxonomy_relevant"] = int(rec.is_taxonomy_relevant)
        row["is_trend_seed"] = int(rec.is_trend_seed)
        row["is_supplementary"] = int(rec.is_supplementary)
        row["needs_comment_fallback"] = int(rec.needs_comment_fallback)
        row["contains_korean_context"] = (
            int(rec.contains_korean_context) if rec.contains_korean_context is not None else None
        )
        row["pii_types"] = json.dumps(rec.pii_types, ensure_ascii=False)
        row["risk_signals"] = json.dumps(rec.risk_signals, ensure_ascii=False)
        row["matched_keywords"] = json.dumps(rec.matched_keywords, ensure_ascii=False)
        row["secondary_flags"] = json.dumps(rec.secondary_flags, ensure_ascii=False)
        row["negative_contexts"] = json.dumps(rec.negative_contexts, ensure_ascii=False)
        row["masking_warnings"] = json.dumps(rec.masking_warnings, ensure_ascii=False)
        row["masked_entities"] = json.dumps(
            [vars(entity) for entity in rec.masked_entities], ensure_ascii=False)
        placeholders = ",".join(f":{name}" for name, _ in _CONTENT_COLS)
        cols = ",".join(name for name, _ in _CONTENT_COLS)
        self.conn.execute(f"INSERT OR REPLACE INTO content_records ({cols}) VALUES ({placeholders})", row)
        self.conn.commit()

    def save_candidate(self, c: UrlCandidate) -> None:
        if not c.candidate_id:
            c.candidate_id = uuid.uuid4().hex
        row = {name: getattr(c, name, None) for name, _ in _CANDIDATE_COLS}
        meta = getattr(c, "meta", {}) or {}
        for name in ("source", "source_type", "board_name", "category_name"):
            row[name] = meta.get(name, row[name])
        row["is_trend_seed"] = int(bool(getattr(c, "is_trend_seed", False)))
        row["is_supplementary"] = int(bool(getattr(c, "is_supplementary", False)))
        cols = ",".join(row)
        values = ",".join(f":{x}" for x in row)
        self.conn.execute(
            f"INSERT OR REPLACE INTO url_candidates ({cols}) VALUES ({values})", row,
        )
        self.conn.commit()

    def find_duplicate(self, rec: ContentRecord, hamming_threshold: int = 3) -> str | None:
        from .dedup import hamming
        exact = self.conn.execute(
            "SELECT content_id FROM content_records WHERE canonical_url=? OR dedup_hash=? LIMIT 1",
            (rec.canonical_url, rec.dedup_hash),
        ).fetchone()
        if exact:
            return exact[0]
        exact = self.conn.execute(
            "SELECT content_id FROM content_records WHERE taxonomy_lv2=? AND subtype=? AND event_key=? LIMIT 1",
            (rec.taxonomy_lv2, rec.subtype, rec.event_key),
        ).fetchone()
        if exact:
            return exact[0]
        if not rec.simhash:
            return None
        # ponytail: (taxonomy,subtype) 내 simhash 전수 스캔 = 삽입당 O(n). 수백~수천 건은 문제 없음.
        # 수만 건↑ 되면 simhash 상위 비트 밴딩(LSH)으로 후보만 좁힐 것.
        for content_id, sh in self.conn.execute(
                "SELECT content_id,simhash FROM content_records WHERE taxonomy_lv2=? AND subtype=? AND simhash IS NOT NULL",
                (rec.taxonomy_lv2, rec.subtype)):
            if sh and hamming(int(rec.simhash), int(sh)) <= hamming_threshold:
                return content_id
        return None

    def log_filter(self, source_url: str, stage: str, status: str, reason: str | None,
                   taxonomy_lv2: str = "", subtype: str = "") -> None:
        self.conn.execute(
            """INSERT INTO filter_logs (source_url,stage,status,reason,taxonomy_lv2,subtype)
            VALUES (?,?,?,?,?,?)""",
            (source_url, stage, status, reason, taxonomy_lv2, subtype),
        )
        self.conn.commit()


def _content_id(rec: ContentRecord) -> str:
    return hashlib.sha1(rec.source_url.encode("utf-8")).hexdigest()[:16]
