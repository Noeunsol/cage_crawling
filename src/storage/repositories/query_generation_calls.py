"""query_generation_calls 테이블: 검색어 생성(OpenAI) 호출 1건당 1행 — DB 전체 비용 집계용."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class GenerationUsageTotal:
    calls: int
    prompt_tokens: int
    completion_tokens: int
    elapsed_s: float


def record_call(
    conn: sqlite3.Connection,
    *,
    run_id: str | None = None,
    taxonomy_lv2: str,
    type_name: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    elapsed_s: float,
    web_search_calls: int = 0,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO query_generation_calls (
                run_id, taxonomy_lv2, type_name, provider, model, prompt_tokens, completion_tokens, elapsed_s,
                web_search_calls
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, taxonomy_lv2, type_name, provider, model, prompt_tokens, completion_tokens, elapsed_s,
             web_search_calls),
        )


def sum_usage(conn: sqlite3.Connection) -> GenerationUsageTotal:
    row = conn.execute(
        """
        SELECT COUNT(*) calls, COALESCE(SUM(prompt_tokens), 0) prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) completion_tokens,
               COALESCE(SUM(elapsed_s), 0) elapsed_s
        FROM query_generation_calls
        """
    ).fetchone()
    return GenerationUsageTotal(
        calls=row["calls"], prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"], elapsed_s=row["elapsed_s"],
    )
