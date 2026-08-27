"""query_executions 테이블: 검색어가 실제로 API에 호출된 기록.

request_fingerprint UNIQUE 제약으로 동일 조건(검색어·provider·기간 등) 재호출을 막는다 (10.1절).
"""

from __future__ import annotations

import json
import sqlite3


def count_by_provider(conn: sqlite3.Connection) -> dict[str, int]:
    """DB 전체에서 실제로 나간 API 호출 수(provider별). 캐시로 건너뛴 건 애초에 행이 안 생긴다."""
    rows = conn.execute(
        """
        SELECT sq.provider AS provider, COUNT(*) c
        FROM query_executions qe JOIN search_queries sq ON sq.id = qe.query_id
        WHERE qe.status = 'success'
        GROUP BY sq.provider
        """
    ).fetchall()
    return {r["provider"]: r["c"] for r in rows}


def get_latest_result_count(conn: sqlite3.Connection, query_id: int) -> int | None:
    """이 검색어의 가장 최근 실행이 실제로 몇 건을 돌려줬는지. 실행 기록이 없으면 None.

    "3. 검색어 생성 및 검토"에서 0건짜리 검색어를 눈에 띄게 보여줘서 교체를 유도하는 데 쓴다.
    """
    row = conn.execute(
        """
        SELECT result_count FROM query_executions
        WHERE query_id = ? AND status = 'success'
        ORDER BY finished_at DESC LIMIT 1
        """,
        (query_id,),
    ).fetchone()
    return row["result_count"] if row else None


def get_by_fingerprint(conn: sqlite3.Connection, request_fingerprint: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM query_executions WHERE request_fingerprint = ?",
        (request_fingerprint,),
    ).fetchone()


def start_execution(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    query_id: int,
    request_params: dict,
    request_fingerprint: str,
) -> tuple[int, bool]:
    """이미 같은 fingerprint로 실행한 적이 있으면 (기존 id, True)를 돌려준다.

    호출부는 already_executed가 True면 API를 다시 부르지 않고 이 실행의 결과를 재사용해야 한다.
    """
    existing = get_by_fingerprint(conn, request_fingerprint)
    if existing is not None:
        return existing["id"], True

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO query_executions (
                run_id, query_id, request_params, request_fingerprint, status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, query_id, json.dumps(request_params, ensure_ascii=False),
             request_fingerprint, "success"),
        )
    return cursor.lastrowid, False


def delete_by_query(conn: sqlite3.Connection, query_id: int) -> int:
    """이 검색어의 실행 기록(=fingerprint)을 지워서 진짜로 재검색될 수 있게 한다.

    중간에 죽어서 검색은 성공했지만 결과가 저장 안 된 경우를 되돌릴 때 쓴다.
    반환값은 지운 행 수.
    """
    with conn:
        cursor = conn.execute("DELETE FROM query_executions WHERE query_id = ?", (query_id,))
    return cursor.rowcount


def finish_execution(
    conn: sqlite3.Connection,
    execution_id: int,
    *,
    status: str,
    result_count: int | None = None,
    credit_usage: dict | None = None,
    error_message: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE query_executions
            SET status = ?, result_count = ?, credit_usage = ?, error_message = ?,
                finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (status, result_count,
             json.dumps(credit_usage, ensure_ascii=False) if credit_usage is not None else None,
             error_message, execution_id),
        )
