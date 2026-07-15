"""Phase 11 — Content DB. 1차는 sqlite (stdlib). 테이블: content_records / url_candidates / filter_logs.

저장 정책 (설계서 §9,§10):
  pass/review → content_records
  모든 fail/pass 이벤트 → filter_logs (stage, reason)
  extraction 실패한 후보 → url_candidates
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from .schema import ContentRecord, UrlCandidate

_SCHEMA = """
CREATE TABLE IF NOT EXISTS content_records (
    content_id TEXT PRIMARY KEY,
    taxonomy_lv2 TEXT, subtype TEXT,
    source_url TEXT, canonical_url TEXT, domain TEXT,
    site_name TEXT, site_type TEXT,
    title TEXT, body_text TEXT,
    published_at TEXT, collected_at TEXT,
    search_query TEXT, search_api TEXT, extractor TEXT, collection_method TEXT,
    language TEXT,
    quality_score REAL, taxonomy_relevance_score REAL, korea_relevance_score REAL,
    filter_status TEXT, filter_reason TEXT, dedup_hash TEXT
);
CREATE TABLE IF NOT EXISTS url_candidates (
    source_url TEXT PRIMARY KEY, domain TEXT,
    search_query TEXT, search_api TEXT,
    taxonomy_lv2_candidate TEXT, subtype_candidate TEXT,
    status TEXT, score REAL
);
CREATE TABLE IF NOT EXISTS filter_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT, stage TEXT, status TEXT, reason TEXT,
    taxonomy_lv2 TEXT, subtype TEXT
);
"""


class Store:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def save_content(self, rec: ContentRecord) -> None:
        if not rec.content_id:
            rec.content_id = _content_id(rec)
        rec.canonical_url = rec.canonical_url or rec.source_url
        self.conn.execute(
            """INSERT OR REPLACE INTO content_records VALUES
            (:content_id,:taxonomy_lv2,:subtype,:source_url,:canonical_url,:domain,
             :site_name,:site_type,:title,:body_text,:published_at,:collected_at,
             :search_query,:search_api,:extractor,:collection_method,:language,:quality_score,
             :taxonomy_relevance_score,:korea_relevance_score,:filter_status,
             :filter_reason,:dedup_hash)""",
            {
                "content_id": rec.content_id, "taxonomy_lv2": rec.taxonomy_lv2,
                "subtype": rec.subtype, "source_url": rec.source_url,
                "canonical_url": rec.canonical_url, "domain": rec.domain,
                "site_name": rec.site_name, "site_type": rec.site_type,
                "title": rec.title, "body_text": rec.body_text,
                "published_at": rec.published_at, "collected_at": rec.collected_at,
                "search_query": rec.search_query, "search_api": rec.search_api,
                "extractor": rec.extractor, "collection_method": rec.collection_method,
                "language": rec.language,
                "quality_score": rec.quality_score,
                "taxonomy_relevance_score": rec.taxonomy_relevance_score,
                "korea_relevance_score": rec.korea_relevance_score,
                "filter_status": rec.filter_status, "filter_reason": rec.filter_reason,
                "dedup_hash": rec.dedup_hash,
            },
        )
        self.conn.commit()

    def save_candidate(self, c: UrlCandidate) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO url_candidates VALUES
            (:source_url,:domain,:search_query,:search_api,
             :taxonomy_lv2_candidate,:subtype_candidate,:status,:score)""",
            {
                "source_url": c.source_url, "domain": c.domain,
                "search_query": c.search_query, "search_api": c.search_api,
                "taxonomy_lv2_candidate": c.taxonomy_lv2_candidate,
                "subtype_candidate": c.subtype_candidate,
                "status": c.status, "score": c.score,
            },
        )
        self.conn.commit()

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
