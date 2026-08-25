"""content_discoveries 테이블: 콘텐츠가 어떤 run/query에서 처음 발견됐는지 기록 (provenance)."""

from __future__ import annotations

import sqlite3


def record_discovery(
    conn: sqlite3.Connection,
    *,
    content_id: int,
    run_id: str,
    query_id: int,
    provider: str,
    returned_url: str,
    rank: int | None = None,
    relevance_score: float | None = None,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO content_discoveries (
                content_id, run_id, query_id, provider, returned_url, rank, relevance_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (content_id, run_id, query_id, provider, returned_url, rank, relevance_score),
        )


def list_for_content(conn: sqlite3.Connection, content_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM content_discoveries WHERE content_id = ?", (content_id,)
    ).fetchall()


def list_by_run(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """이번 실행에서 실제로 저장된 콘텐츠 목록 (결과 화면 표용).

    content_discoveries는 lv2/type을 직접 갖고 있지 않아 search_queries를 통해 가져온다.
    """
    return conn.execute(
        """
        SELECT
            c.id AS content_id, c.title, c.content, c.published_date, c.canonical_url,
            c.source_domain, c.source_category, c.status, c.collected_at,
            sq.taxonomy_lv2, sq.type_name, sq.provider,
            m.decision, m.decision_reason
        FROM content_discoveries cd
        JOIN search_queries sq ON sq.id = cd.query_id
        JOIN contents c ON c.id = cd.content_id
        LEFT JOIN content_taxonomy_mappings m
            ON m.content_id = c.id AND m.taxonomy_lv2 = sq.taxonomy_lv2 AND m.type_name = sq.type_name
        WHERE cd.run_id = ?
        ORDER BY cd.discovered_at
        """,
        (run_id,),
    ).fetchall()
