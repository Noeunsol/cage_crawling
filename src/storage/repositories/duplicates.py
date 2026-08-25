"""content_duplicates 테이블: URL은 다르지만 같은 본문/사건으로 판단된 콘텐츠 (10.3절)."""

from __future__ import annotations

import sqlite3


def record_duplicate(
    conn: sqlite3.Connection,
    *,
    representative_content_id: int,
    duplicate_reason: str,
    duplicate_content_id: int | None = None,
    duplicate_url: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO content_duplicates (
                representative_content_id, duplicate_content_id, duplicate_url, duplicate_reason
            ) VALUES (?, ?, ?, ?)
            """,
            (representative_content_id, duplicate_content_id, duplicate_url, duplicate_reason),
        )


def list_for_representative(conn: sqlite3.Connection, content_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM content_duplicates WHERE representative_content_id = ?", (content_id,)
    ).fetchall()
