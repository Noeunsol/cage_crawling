"""중복 URL/본문 제외 (11.2절 1, 2번). 실제 중복 판정은 extraction/duplicates.py가 한다."""

from __future__ import annotations

import sqlite3

from src.extraction import duplicates
from src.filtering.pipeline import FilterContext, FilterOutcome


def check(ctx: FilterContext, conn: sqlite3.Connection) -> FilterOutcome:
    url_dup = duplicates.check_url_duplicate(conn, ctx.canonical_url)
    if url_dup.is_duplicate:
        return FilterOutcome(passed=False, reason="duplicate", detail="이미 저장된 URL과 동일합니다.")

    content_dup = duplicates.check_content_duplicate(conn, ctx.content_hash)
    if content_dup.is_duplicate:
        return FilterOutcome(
            passed=False, reason="duplicate", detail="이미 저장된 콘텐츠와 본문이 동일합니다.",
        )
    return FilterOutcome(passed=True)
