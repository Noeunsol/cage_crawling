"""Phase 4 — URL-level 1차 필터. 본문 추출 전 title/snippet/url/domain만으로 걸러 비용 절감.

Firecrawl/Crawl4AI/LLM 호출 전에 명백히 무관한 후보를 제거한다.
"""
from __future__ import annotations

import re

from .policy import Subtype
from .schema import FilterResult, UrlCandidate
from .site_registry import SiteRegistry

_HANGUL = re.compile(r"[가-힣]")


class UrlFilter:
    def __init__(self, registry: SiteRegistry, date_range: dict | None = None):
        self.registry = registry
        self.date_range = date_range or {}

    def check(self, c: UrlCandidate, subtype: Subtype) -> FilterResult:
        text = f"{c.title or ''} {c.snippet or ''}"

        # 제외어
        for neg in subtype.negative_patterns:
            if neg in text:
                return FilterResult("fail", f"negative_pattern:{neg}")

        # keyword 게이트는 keyword 경로에만 적용.
        # semantic/site_sampling 등은 일부러 키워드가 없을 수 있어 matcher가 분류를 맡는다.
        if c.collection_method == "keyword":
            terms = subtype.keywords + subtype.positive_patterns
            if terms and not any(t in text for t in terms):
                return FilterResult("fail", "no_keyword_match")

        # 한국 관련성: 알려진 한국 사이트이거나 한글 포함
        known = self.registry.lookup(c.domain).site_name != "unknown"
        if not known and not _HANGUL.search(text):
            return FilterResult("fail", "not_korea_relevant")

        # 날짜 조건: 힌트가 있을 때만 검사. 없으면 통과(추출 후 재판단, published_at 누락 허용)
        if c.published_at_hint and not self._in_range(c.published_at_hint):
            return FilterResult("fail", f"out_of_date_range:{c.published_at_hint}")

        return FilterResult("pass", None)

    def _in_range(self, date_str: str) -> bool:
        after = self.date_range.get("after")
        before = self.date_range.get("before")
        if after and date_str < after:
            return False
        if before and date_str > before:
            return False
        return True
