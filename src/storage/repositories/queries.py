"""search_queries 테이블: provider별로 분리된 검색어와 그 상태 이력."""

from __future__ import annotations

import sqlite3


def create_query(
    conn: sqlite3.Connection,
    *,
    taxonomy_lv2: str,
    type_name: str,
    provider: str,
    query_text: str,
    status: str,
    created_by: str,
    parent_query_id: int | None = None,
    prompt_version: str | None = None,
    model: str | None = None,
) -> int:
    """(provider, lv2, type, query_text)가 이미 있으면 새로 만들지 않고 기존 id를 돌려준다."""
    existing = conn.execute(
        """
        SELECT id FROM search_queries
        WHERE provider = ? AND taxonomy_lv2 = ? AND type_name = ? AND query_text = ?
        """,
        (provider, taxonomy_lv2, type_name, query_text),
    ).fetchone()
    if existing is not None:
        return existing["id"]

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO search_queries (
                taxonomy_lv2, type_name, provider, query_text, status,
                parent_query_id, created_by, prompt_version, model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (taxonomy_lv2, type_name, provider, query_text, status,
             parent_query_id, created_by, prompt_version, model),
        )
    return cursor.lastrowid


def update_status(conn: sqlite3.Connection, query_id: int, status: str) -> None:
    with conn:
        conn.execute(
            "UPDATE search_queries SET status = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (status, query_id),
        )


def get_query(conn: sqlite3.Connection, query_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM search_queries WHERE id = ?", (query_id,)).fetchone()


def list_queries(
    conn: sqlite3.Connection,
    *,
    taxonomy_lv2: str | None = None,
    type_name: str | None = None,
    provider: str | None = None,
    status: str | None = None,
) -> list[sqlite3.Row]:
    """조건에 맞는 검색어 목록. 인자를 안 주면 전체를 반환한다."""
    clauses, params = [], []
    for column, value in (
        ("taxonomy_lv2", taxonomy_lv2), ("type_name", type_name),
        ("provider", provider), ("status", status),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(f"SELECT * FROM search_queries {where}", params).fetchall()
