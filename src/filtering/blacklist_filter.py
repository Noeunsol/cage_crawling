"""블랙리스트 도메인 제외 (11.2절 7번)."""

from __future__ import annotations

from src.filtering.pipeline import FilterContext, FilterOutcome


def check(ctx: FilterContext, blacklist_domains: list[str]) -> FilterOutcome:
    if ctx.source_domain in blacklist_domains:
        return FilterOutcome(
            passed=False, reason="blacklisted_domain",
            detail=f"'{ctx.source_domain}'는 블랙리스트 도메인입니다.",
        )
    return FilterOutcome(passed=True)
