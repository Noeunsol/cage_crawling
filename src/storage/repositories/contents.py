"""contents 테이블: 최종 저장 콘텐츠(accepted/excluded/failed 모두 포함)."""

from __future__ import annotations

import sqlite3


def get_by_canonical_url(conn: sqlite3.Connection, canonical_url: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM contents WHERE canonical_url = ?", (canonical_url,)
    ).fetchone()


def upsert_content(
    conn: sqlite3.Connection,
    *,
    title: str,
    content: str,
    published_date: str | None,
    canonical_url: str,
    source_name: str | None,
    source_domain: str,
    source_category: str | None,
    status: str,
    content_hash: str,
) -> tuple[int, bool]:
    """canonical_url이 이미 있으면 본문은 다시 쓰지 않고 last_discovered_at만 갱신한다.

    (10.2절: "중복 URL은 본문을 다시 저장하지 않는다"). 반환값은 (content_id, created 여부).
    """
    existing = get_by_canonical_url(conn, canonical_url)
    if existing is not None:
        with conn:
            conn.execute(
                "UPDATE contents SET last_discovered_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ?",
                (existing["id"],),
            )
        return existing["id"], False

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO contents (
                title, content, published_date, canonical_url,
                source_name, source_domain, source_category, status,
                collected_at, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)
            """,
            (title, content, published_date, canonical_url,
             source_name, source_domain, source_category, status, content_hash),
        )
    return cursor.lastrowid, True


def list_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM contents WHERE status = ?", (status,)).fetchall()


def get_by_content_hash(conn: sqlite3.Connection, content_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM contents WHERE content_hash = ? LIMIT 1", (content_hash,)
    ).fetchone()
