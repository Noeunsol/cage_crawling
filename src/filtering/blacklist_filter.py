"""블랙리스트 도메인 제외."""

from __future__ import annotations

from src.filtering.pipeline import FilterContext, FilterOutcome
from src.utils.urls import is_blocklisted_domain


def check(ctx: FilterContext, blacklist_domains: list[str]) -> FilterOutcome:
    if is_blocklisted_domain(ctx.source_domain, blacklist_domains):
        return FilterOutcome(
            passed=False, reason="blacklisted_domain",
            detail=f"'{ctx.source_domain}'는 블랙리스트 도메인입니다.",
        )
    return FilterOutcome(passed=True)
