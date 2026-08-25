"""discarded_candidates 테이블: 제외/실패 후보. 본문은 저장하지 않는다 (11.4절)."""

from __future__ import annotations

import sqlite3


def record_discarded(
    conn: sqlite3.Connection,
    *,
    original_url: str,
    normalized_url: str,
    run_id: str | None,
    query_id: int | None,
    source_domain: str | None,
    reason: str,
    retryable: bool,
    requested_date_from: str | None = None,
    requested_date_to: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO discarded_candidates (
                original_url, normalized_url, run_id, query_id, source_domain,
                reason, retryable, requested_date_from, requested_date_to
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (original_url, normalized_url, run_id, query_id, source_domain,
             reason, int(retryable), requested_date_from, requested_date_to),
        )


def list_by_normalized_url(conn: sqlite3.Connection, normalized_url: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM discarded_candidates WHERE normalized_url = ?", (normalized_url,)
    ).fetchall()


def list_retryable(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM discarded_candidates WHERE run_id = ? AND retryable = 1", (run_id,)
    ).fetchall()


def list_by_run(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM discarded_candidates WHERE run_id = ?", (run_id,)
    ).fetchall()
