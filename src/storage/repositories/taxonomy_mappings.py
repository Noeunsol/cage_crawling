"""content_taxonomy_mappings 테이블: 콘텐츠 하나가 여러 LV2/type과 연결될 수 있다."""

from __future__ import annotations

import sqlite3


def add_mapping(
    conn: sqlite3.Connection,
    *,
    content_id: int,
    taxonomy_lv2: str,
    type_name: str,
    decision: str,
    decision_reason: str | None,
    prompt_name: str | None,
    prompt_version: str | None,
    model: str | None,
) -> None:
    """(content_id, taxonomy_lv2, type_name) UNIQUE 제약 덕분에 재실행해도 중복 저장되지 않는다."""
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO content_taxonomy_mappings (
                content_id, taxonomy_lv2, type_name, decision, decision_reason,
                prompt_name, prompt_version, model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (content_id, taxonomy_lv2, type_name, decision, decision_reason,
             prompt_name, prompt_version, model),
        )


def list_for_content(conn: sqlite3.Connection, content_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM content_taxonomy_mappings WHERE content_id = ?", (content_id,)
    ).fetchall()


def list_with_content(
    conn: sqlite3.Connection,
    *,
    taxonomy_lv2: str | None = None,
    type_name: str | None = None,
    decision: str | None = None,
) -> list[sqlite3.Row]:
    """DB 전체를 훑는 데이터 탐색 화면용: 콘텐츠 + 매핑 + (처음 발견한) provider를 한 번에 준다."""
    clauses, params = [], []
    for column, value in (
        ("m.taxonomy_lv2", taxonomy_lv2), ("m.type_name", type_name), ("m.decision", decision),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    return conn.execute(
        f"""
        SELECT
            c.id AS content_id, c.title, c.content, c.published_date, c.canonical_url,
            c.source_domain, c.status, c.collected_at,
            m.taxonomy_lv2, m.type_name, m.decision, m.decision_reason,
            (SELECT cd.provider FROM content_discoveries cd
             WHERE cd.content_id = c.id ORDER BY cd.discovered_at LIMIT 1) AS provider
        FROM content_taxonomy_mappings m
        JOIN contents c ON c.id = m.content_id
        {where}
        ORDER BY c.collected_at DESC
        """,
        params,
    ).fetchall()
