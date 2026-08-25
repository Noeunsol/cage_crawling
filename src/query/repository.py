"""생성된 검색어를 DB에 저장하는 얇은 wrapper. SQL은 storage/repositories/queries.py가 전담한다."""

from __future__ import annotations

import sqlite3

from src.storage.repositories import queries as queries_repo
from src.storage.repositories import query_executions as exec_repo


def save_generated_queries(
    conn: sqlite3.Connection,
    *,
    taxonomy_lv2: str,
    type_name: str,
    provider: str,
    query_texts: list[str],
    prompt_version: str,
    model: str,
) -> list[int]:
    """status=generated, created_by=openai로 저장한다. 이미 있는 텍스트는 새로 만들지 않는다."""
    return [
        queries_repo.create_query(
            conn,
            taxonomy_lv2=taxonomy_lv2,
            type_name=type_name,
            provider=provider,
            query_text=text,
            status="generated",
            created_by="openai",
            prompt_version=prompt_version,
            model=model,
        )
        for text in query_texts
    ]


# 실행에 반영되는 상태: 새로 생성됨 / 사용자가 고침 / 사용자가 직접 추가. rejected/unused/used는 제외.
ACTIVE_STATUSES = ("generated", "user_edited", "user_created")


def list_active_queries(
    conn: sqlite3.Connection, *, taxonomy_lv2: str, type_name: str, provider: str
) -> list:
    return [
        row for row in queries_repo.list_queries(
            conn, taxonomy_lv2=taxonomy_lv2, type_name=type_name, provider=provider,
        )
        if row["status"] in ACTIVE_STATUSES
    ]


def list_used_queries(
    conn: sqlite3.Connection, *, taxonomy_lv2: str, type_name: str, provider: str
) -> list:
    return queries_repo.list_queries(
        conn, taxonomy_lv2=taxonomy_lv2, type_name=type_name, provider=provider, status="used",
    )


def reactivate_query(conn: sqlite3.Connection, query_id: int) -> None:
    """used 검색어를 다시 generated로 되돌리고, 기록된 검색 fingerprint도 지워 진짜 재검색되게 한다.

    검색은 성공했는데 그 뒤 처리가 죽어서 결과가 저장 안 됐을 때 쓴다 — 안 지우면 다음 실행이
    "이미 검색했다"고 착각하고 그 검색어 몫을 영영 건너뛴다.
    """
    exec_repo.delete_by_query(conn, query_id)
    queries_repo.update_status(conn, query_id, "generated")


def add_manual_query(
    conn: sqlite3.Connection, *, taxonomy_lv2: str, type_name: str, provider: str, query_text: str
) -> int:
    return queries_repo.create_query(
        conn, taxonomy_lv2=taxonomy_lv2, type_name=type_name, provider=provider,
        query_text=query_text, status="user_created", created_by="user",
    )


def edit_query(conn: sqlite3.Connection, original_query_id: int, new_text: str) -> int:
    """원본은 rejected로 남기고(이력 보존, 5.4절), 수정본을 새 행으로 만든다."""
    original = queries_repo.get_query(conn, original_query_id)
    new_id = queries_repo.create_query(
        conn, taxonomy_lv2=original["taxonomy_lv2"], type_name=original["type_name"],
        provider=original["provider"], query_text=new_text, status="user_edited",
        created_by="user", parent_query_id=original_query_id,
    )
    queries_repo.update_status(conn, original_query_id, "rejected")
    return new_id


def reject_query(conn: sqlite3.Connection, query_id: int) -> None:
    queries_repo.update_status(conn, query_id, "rejected")
