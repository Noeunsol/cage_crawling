"""URL/본문 중복 체크 (10.2, 10.3절). 실제로 저장할지 말지는 호출부(파이프라인, Phase 9~10)가 정한다."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.storage.repositories import contents as contents_repo


@dataclass
class DuplicateCheck:
    is_duplicate: bool
    reason: str | None = None            # "same_url" / "same_content_hash"
    existing_content_id: int | None = None


def check_url_duplicate(conn: sqlite3.Connection, normalized_url: str) -> DuplicateCheck:
    existing = contents_repo.get_by_canonical_url(conn, normalized_url)
    if existing is None:
        return DuplicateCheck(is_duplicate=False)
    return DuplicateCheck(is_duplicate=True, reason="same_url", existing_content_id=existing["id"])


def check_content_duplicate(conn: sqlite3.Connection, content_hash: str) -> DuplicateCheck:
    existing = contents_repo.get_by_content_hash(conn, content_hash)
    if existing is None:
        return DuplicateCheck(is_duplicate=False)
    return DuplicateCheck(
        is_duplicate=True, reason="same_content_hash", existing_content_id=existing["id"],
    )
