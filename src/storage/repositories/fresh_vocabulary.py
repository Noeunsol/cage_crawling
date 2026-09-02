"""fresh_vocabulary_cache 테이블: 웹서치로 얻은 type별 최근 표현을 TTL 동안 재사용한다."""

from __future__ import annotations

import json
import sqlite3

_TTL_HOURS = 48  # 고정값. type별 차등 TTL이 필요해지면 그때 컬럼/설정 추가


def get_cached(conn: sqlite3.Connection, taxonomy_lv2: str, type_name: str) -> list[str] | None:
    row = conn.execute(
        """
        SELECT terms FROM fresh_vocabulary_cache
        WHERE taxonomy_lv2 = ? AND type_name = ? AND fetched_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)
        """,
        (taxonomy_lv2, type_name, f"-{_TTL_HOURS} hours"),
    ).fetchone()
    return json.loads(row["terms"]) if row else None


def save(conn: sqlite3.Connection, taxonomy_lv2: str, type_name: str, terms: list[str]) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO fresh_vocabulary_cache (taxonomy_lv2, type_name, terms)
            VALUES (?, ?, ?)
            ON CONFLICT (taxonomy_lv2, type_name)
            DO UPDATE SET terms = excluded.terms, fetched_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (taxonomy_lv2, type_name, json.dumps(terms, ensure_ascii=False)),
        )
