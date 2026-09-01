"""near_duplicate_observations 테이블: 근사중복 shadow mode 관측 기록."""

from __future__ import annotations

import sqlite3


def record_observation(
    conn: sqlite3.Connection, *,
    content_id: int, matched_content_id: int,
    title_similarity: float, content_similarity: float, would_exclude: bool,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO near_duplicate_observations
                (content_id, matched_content_id, title_similarity, content_similarity, would_exclude)
            VALUES (?, ?, ?, ?, ?)
            """,
            (content_id, matched_content_id, title_similarity, content_similarity, int(would_exclude)),
        )


def list_unlabeled(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    query = "SELECT * FROM near_duplicate_observations WHERE human_label IS NULL ORDER BY content_similarity DESC"
    if limit:
        query += f" LIMIT {int(limit)}"
    return conn.execute(query).fetchall()


def set_label(conn: sqlite3.Connection, observation_id: int, human_label: str) -> None:
    with conn:
        conn.execute(
            "UPDATE near_duplicate_observations SET human_label = ? WHERE id = ?",
            (human_label, observation_id),
        )
