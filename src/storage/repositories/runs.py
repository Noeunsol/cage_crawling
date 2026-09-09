"""collection_runs 테이블: 한 번의 수집 실행 단위."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4


def new_run_id() -> str:
    """삭제된 실행 번호나 동시 실행과 충돌하지 않는 새 run ID를 만든다."""
    return f"run-{uuid4().hex}"


def elapsed_seconds(run_row, now: datetime | None = None) -> float:
    """DB 실행 기록의 시작~종료 시간을 초 단위로 반환한다."""
    started_at = datetime.fromisoformat(run_row["started_at"].replace("Z", "+00:00"))
    finished_at = run_row["finished_at"]
    ended_at = (
        datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        if finished_at else (now or datetime.now(timezone.utc))
    )
    return max((ended_at - started_at).total_seconds(), 0.0)


def create_run(conn: sqlite3.Connection, run_id: str, settings_snapshot: dict) -> None:
    """새 실행을 status=running으로 기록한다."""
    with conn:
        conn.execute(
            "INSERT INTO collection_runs (run_id, settings_snapshot, status) VALUES (?, ?, ?)",
            (run_id, json.dumps(settings_snapshot, ensure_ascii=False), "running"),
        )


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    provider_usage_summary: dict,
    warning_summary: list[str],
) -> None:
    """실행을 종료 상태(completed/stopped/failed)로 갱신한다."""
    with conn:
        conn.execute(
            """
            UPDATE collection_runs
            SET status = ?, finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                provider_usage_summary = ?, warning_summary = ?
            WHERE run_id = ?
            """,
            (
                status,
                json.dumps(provider_usage_summary, ensure_ascii=False),
                json.dumps(warning_summary, ensure_ascii=False),
                run_id,
            ),
        )


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM collection_runs WHERE run_id = ?", (run_id,)
    ).fetchone()


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """최근 실행부터 최대 limit개 (실행 기록 목록 화면용)."""
    return conn.execute(
        "SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()


def sum_provider_calls(conn: sqlite3.Connection) -> dict[str, int]:
    """모든 실행의 provider_usage_summary를 합쳐 provider별 총 호출 수를 낸다.

    tavily/serpapi 실제 호출 수는 query_executions에서 더 정확하게 셀 수 있지만(캐시 제외),
    openai_filter가 쓴 openai 호출은 여기(provider_usage_summary["openai"])에만 남아있다.
    """
    totals: dict[str, int] = {}
    for row in conn.execute("SELECT provider_usage_summary FROM collection_runs").fetchall():
        if not row["provider_usage_summary"]:
            continue
        usage = json.loads(row["provider_usage_summary"])
        for provider, calls in usage.items():
            totals[provider] = totals.get(provider, 0) + len(calls)
    return totals


def sum_openai_filter_usage(conn: sqlite3.Connection) -> dict:
    """모든 실행에서 openai_filter가 실제로 쓴 토큰/시간 합계."""
    prompt_tokens = completion_tokens = 0
    elapsed_s = 0.0
    calls = 0
    for row in conn.execute("SELECT provider_usage_summary FROM collection_runs").fetchall():
        if not row["provider_usage_summary"]:
            continue
        for c in json.loads(row["provider_usage_summary"]).get("openai", []):
            calls += 1
            prompt_tokens += c.get("prompt_tokens", 0)
            completion_tokens += c.get("completion_tokens", 0)
            elapsed_s += c.get("elapsed_s", 0)
    return {"calls": calls, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "elapsed_s": elapsed_s}
