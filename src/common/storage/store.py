"""Phase 11 — Content DB (sqlite). content_records / url_candidates / filter_logs.

스키마 변경은 PRAGMA user_version + ALTER ADD COLUMN(idempotent)으로 마이그레이션.
기존 DB를 파괴하지 않고 신규 컬럼을 추가한다. --reset-db로 재생성 가능.
raw_text는 정제 전 원문 — export에서는 기본 제외(report.export_csv 참고).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from src.common.schema import ContentRecord, UrlCandidate, canonicalize_url, content_id_for

_SCHEMA_VERSION = 23

# (컬럼명, 타입) — CREATE와 마이그레이션 공용
_CONTENT_COLS = [
    ("content_id", "TEXT PRIMARY KEY"),
    ("taxonomy_lv1", "TEXT"), ("taxonomy_lv2", "TEXT"), ("subtype", "TEXT"),
    # target(검색 시 의도한 LV2) vs predicted(taxonomy_lv2) 분리 저장 — 2차 target-match 비교용
    ("taxonomy_lv2_candidate", "TEXT"), ("subtype_candidate", "TEXT"),
    ("source_url", "TEXT"), ("canonical_url", "TEXT"), ("domain", "TEXT"),
    ("site_name", "TEXT"), ("site_type", "TEXT"),
    ("title", "TEXT"), ("body_text", "TEXT"),
    ("raw_text", "TEXT"), ("cleaned_text", "TEXT"),
    # v19 Q&A 구조화 본문. core_text는 화면·품질·LLM의 기본 본문이다.
    ("question_body", "TEXT"), ("answer_body", "TEXT"), ("core_text", "TEXT"),
    ("published_at", "TEXT"), ("published_at_source", "TEXT"), ("collected_at", "TEXT"),
    ("search_query", "TEXT"), ("search_api", "TEXT"), ("extractor", "TEXT"),
    ("collection_type", "TEXT"), ("discovery_method", "TEXT"),
    ("language", "TEXT"), ("korean_language_ratio", "REAL"),
    ("quality_score", "REAL"), ("taxonomy_relevance_score", "REAL"), ("korea_relevance_score", "REAL"),
    ("korea_context_evidence", "TEXT"),
    ("harmfulness_score", "REAL"), ("taxonomy_fit_score", "REAL"),
    ("seed_source_value_score", "REAL"),
    ("value_score", "REAL"), ("extraction_likelihood", "REAL"),
    ("filter_status", "TEXT"), ("filter_reason", "TEXT"), ("llm_escalation_reason", "TEXT"),
    ("dedup_hash", "TEXT"), ("simhash", "TEXT"), ("event_key", "TEXT"), ("duplicate_of", "TEXT"),
    # v7 트렌드 수집 모드
    ("source", "TEXT"), ("source_type", "TEXT"), ("board_name", "TEXT"), ("category_name", "TEXT"),
    ("category", "TEXT"), ("is_risk_candidate", "INTEGER"),
    ("view_count", "INTEGER"), ("like_count", "INTEGER"), ("comment_count", "INTEGER"),
    ("is_trending", "INTEGER"),
    ("risk_score", "INTEGER"), ("trend_score", "INTEGER"), ("confidence", "INTEGER"), ("action", "TEXT"),
    ("is_taxonomy_relevant", "INTEGER"), ("is_trend_seed", "INTEGER"),
    ("filter_action", "TEXT"), ("negative_contexts", "TEXT"), ("needs_comment_fallback", "INTEGER"),
    # v8 후처리/분류 부가정보
    ("risk_signals", "TEXT"), ("matched_keywords", "TEXT"), ("secondary_flags", "TEXT"),
    ("classification_source", "TEXT"), ("classification_reason", "TEXT"),
    ("llm_model", "TEXT"), ("llm_input_tokens", "INTEGER"),
    ("llm_cached_input_tokens", "INTEGER"), ("llm_output_tokens", "INTEGER"),
    ("llm_total_tokens", "INTEGER"), ("llm_estimated_cost_usd", "REAL"),
    ("is_harmful", "INTEGER"), ("concrete_context_score", "REAL"),
    ("evidence_spans", "TEXT"),
    ("contains_korean_context", "INTEGER"),
    ("crawl_status", "TEXT"),
    ("parent_source_url", "TEXT"), ("link_source", "TEXT"), ("is_supplementary", "INTEGER"),
    # v18 2차 semantic discovery provenance
    ("run_id", "TEXT"), ("taxonomy_run_id", "TEXT"), ("collection_phase", "INTEGER"), ("query_id", "TEXT"),
    ("discovery_provider", "TEXT"), ("discovery_query", "TEXT"), ("discovery_relevance_score", "REAL"),
    # v21 2차 targeted acceptance gate
    ("target_type", "TEXT"), ("source_id", "TEXT"), ("query_plan_id", "TEXT"),
    ("korea_relevance_type", "TEXT"), ("korea_evidence", "TEXT"), ("lv2_evidence", "TEXT"),
    ("is_official_seed", "INTEGER"),
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
    ("harm_signal_url_score", "REAL"),
    ("llm_model", "TEXT"), ("llm_input_tokens", "INTEGER"),
    ("llm_cached_input_tokens", "INTEGER"), ("llm_output_tokens", "INTEGER"),
    ("llm_total_tokens", "INTEGER"), ("llm_estimated_cost_usd", "REAL"),
    ("filter_reason", "TEXT"), ("status", "TEXT"), ("score", "REAL"),
    # v18 2차 semantic discovery provenance + rerank 메타(content_hint는 후보에만 보관)
    ("run_id", "TEXT"), ("taxonomy_run_id", "TEXT"), ("collection_phase", "INTEGER"), ("query_id", "TEXT"),
    ("discovery_provider", "TEXT"), ("discovery_query", "TEXT"),
    ("discovery_relevance_score", "REAL"), ("korea_relevance_score", "REAL"),
    ("content_hint", "TEXT"),
    # v21 query plan provenance
    ("target_type", "TEXT"), ("source_id", "TEXT"), ("source_access", "TEXT"),
    ("query_plan_id", "TEXT"), ("query_generation_source", "TEXT"),
    ("expected_korea_evidence", "TEXT"), ("expected_lv2_evidence", "TEXT"),
    ("korea_evidence", "TEXT"), ("lv2_evidence", "TEXT"),
]

_OTHER_SCHEMA = """
CREATE TABLE IF NOT EXISTS filter_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT, stage TEXT, status TEXT, reason TEXT,
    taxonomy_lv2 TEXT, subtype TEXT
);
-- 2차 targeted 검색 계획. 계획 재사용 판단(seed_fingerprint/created_at/성과)과
-- query별 성과 리포트를 한 테이블로 겸한다.
CREATE TABLE IF NOT EXISTS query_plans (
    plan_id TEXT PRIMARY KEY,
    lv2 TEXT, target_type TEXT, query TEXT, query_kind TEXT, source_id TEXT,
    expected_korea_evidence TEXT, expected_lv2_evidence TEXT,
    seed_fingerprint TEXT, generation_source TEXT, created_at TEXT, seed_url TEXT,
    discovered_count INTEGER DEFAULT 0, fetch_success_count INTEGER DEFAULT 0,
    domestic_pass_count INTEGER DEFAULT 0, lv2_pass_count INTEGER DEFAULT 0,
    stored_count INTEGER DEFAULT 0
);
"""

_PLAN_STAT_FIELDS = (
    "discovered_count", "fetch_success_count",
    "domestic_pass_count", "lv2_pass_count", "stored_count",
)


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
        # v23: 2차 레코드에까지 붙던 trend_ 접두어를 걷어낸다.
        # 1차/2차 구분은 collection_phase 컬럼이 담당하므로 상태값은 모드 중립이어야 한다.
        self.conn.execute(
            "UPDATE url_candidates SET status = REPLACE(status,'trend_','') WHERE status LIKE 'trend_%'")
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
        row["is_risk_candidate"] = int(rec.is_risk_candidate)
        row["is_trending"] = int(rec.is_trending)
        row["is_taxonomy_relevant"] = int(rec.is_taxonomy_relevant)
        row["is_trend_seed"] = int(rec.is_trend_seed)
        row["is_supplementary"] = int(rec.is_supplementary)
        row["needs_comment_fallback"] = int(rec.needs_comment_fallback)
        row["is_official_seed"] = int(rec.is_official_seed)
        row["korea_evidence"] = json.dumps(rec.korea_evidence, ensure_ascii=False)
        row["lv2_evidence"] = json.dumps(rec.lv2_evidence, ensure_ascii=False)
        row["contains_korean_context"] = (
            int(rec.contains_korean_context) if rec.contains_korean_context is not None else None
        )
        row["is_harmful"] = int(rec.is_harmful) if rec.is_harmful is not None else None
        row["risk_signals"] = json.dumps(rec.risk_signals, ensure_ascii=False)
        row["matched_keywords"] = json.dumps(rec.matched_keywords, ensure_ascii=False)
        row["secondary_flags"] = json.dumps(rec.secondary_flags, ensure_ascii=False)
        row["korea_context_evidence"] = json.dumps(rec.korea_context_evidence, ensure_ascii=False)
        row["evidence_spans"] = json.dumps(rec.evidence_spans, ensure_ascii=False)
        row["negative_contexts"] = json.dumps(rec.negative_contexts, ensure_ascii=False)
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
        for name in ("expected_korea_evidence", "expected_lv2_evidence",
                     "korea_evidence", "lv2_evidence"):
            row[name] = json.dumps(getattr(c, name, []) or [], ensure_ascii=False)
        cols = ",".join(row)
        values = ",".join(f":{x}" for x in row)
        self.conn.execute(
            f"INSERT OR REPLACE INTO url_candidates ({cols}) VALUES ({values})", row,
        )
        self.conn.commit()

    def save_query_plans(self, plans: list[dict]) -> None:
        """검색 계획 upsert. 이미 있는 plan_id의 성과 카운터는 보존한다."""
        cols = ("plan_id", "lv2", "target_type", "query", "query_kind", "source_id",
                "expected_korea_evidence", "expected_lv2_evidence",
                "seed_fingerprint", "generation_source", "created_at", "seed_url")
        rows = [
            tuple(
                json.dumps(plan.get(name, []), ensure_ascii=False)
                if name.startswith("expected_") else plan.get(name, "")
                for name in cols
            )
            for plan in plans
        ]
        self.conn.executemany(
            f"INSERT INTO query_plans ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(plan_id) DO UPDATE SET "
            + ",".join(f"{name}=excluded.{name}" for name in cols if name != "plan_id"),
            rows,
        )
        self.conn.commit()

    def load_query_plans(self, lv2: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM query_plans WHERE lv2=? ORDER BY created_at DESC, plan_id", (lv2,))
        names = [d[0] for d in cur.description]
        plans = [dict(zip(names, row)) for row in cur]
        for plan in plans:
            for name in ("expected_korea_evidence", "expected_lv2_evidence"):
                plan[name] = json.loads(plan[name] or "[]")
        return plans

    def bump_plan_stat(self, plan_id: str, field: str, delta: int = 1) -> None:
        if not plan_id or field not in _PLAN_STAT_FIELDS:
            return
        self.conn.execute(
            f"UPDATE query_plans SET {field}=COALESCE({field},0)+? WHERE plan_id=?", (delta, plan_id))
        self.conn.commit()

    def find_duplicate(self, rec: ContentRecord, hamming_threshold: int = 3) -> str | None:
        from src.common.storage.dedup import hamming
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

    def existing_canonical_urls(self) -> set[str]:
        """저장된 콘텐츠의 canonical URL 집합. 2차 pre-fetch 중복 제거용(인덱스 있음)."""
        return {
            row[0]
            for row in self.conn.execute(
                "SELECT canonical_url FROM content_records WHERE canonical_url IS NOT NULL"
            )
        }

    def robots_disallowed_domains(self) -> list[str]:
        """이미 robots 정책으로 수집 불가가 확인된 도메인. 다음 discovery에서 제외한다."""
        return [row[0] for row in self.conn.execute(
            """SELECT DISTINCT domain FROM url_candidates
               WHERE status='extraction_failed' AND filter_reason='robots_disallowed'
                 AND domain IS NOT NULL AND domain!='' ORDER BY domain"""
        )]

    def processed_url_keys(self) -> set[str]:
        """1차 수집에서 최종 처리된 URL. 실패·표본 미선택 항목은 재시도한다."""
        urls = [row[0] for row in self.conn.execute(
            "SELECT COALESCE(canonical_url,source_url) FROM content_records"
        )]
        urls.extend(row[0] for row in self.conn.execute(
            """SELECT COALESCE(canonical_url,source_url) FROM url_candidates
               WHERE status IN ('prefilter_discarded','discard','accepted',
                                'duplicate','supplementary_collected')"""
        ))
        return {canonicalize_url(url) for url in urls if url}

    def log_filter(self, source_url: str, stage: str, status: str, reason: str | None,
                   taxonomy_lv2: str = "", subtype: str = "") -> None:
        self.conn.execute(
            """INSERT INTO filter_logs (source_url,stage,status,reason,taxonomy_lv2,subtype)
            VALUES (?,?,?,?,?,?)""",
            (source_url, stage, status, reason, taxonomy_lv2, subtype),
        )
        self.conn.commit()


def _content_id(rec: ContentRecord) -> str:
    return content_id_for(rec.source_url)
