"""Phase 4 — URL-level 필터. 본문 추출 전 title/snippet/url/domain만으로 비용 절감.

filter_mode (taxonomy별):
  minimal  — 중복/광고/명백 무관만 (hard-negative + 한국관련성 + 날짜). 키워드 게이트 완화.
  balanced — + negative_patterns + keyword 게이트(keyword 경로). 기본값.
  strict   — + 경로 무관 keyword/positive relevance 강제.
hard-negative는 mode 무관 항상 적용 (minimal ≠ 무필터).
"""
from __future__ import annotations

import re

from .policy import Subtype
from .schema import FilterResult, UrlCandidate
from .site_registry import SiteRegistry

_HANGUL = re.compile(r"[가-힣]")


class UrlFilter:
    def __init__(self, registry: SiteRegistry, date_range: dict | None = None,
                 filtering: dict | None = None):
        self.registry = registry
        self.date_range = date_range or {}
        f = filtering or {}
        self.mode_by_taxonomy = f.get("mode_by_taxonomy", {})
        self.hard_negatives = f.get("hard_negative_patterns", [])

    def check(self, c: UrlCandidate, subtype: Subtype, taxonomy_lv2: str = "") -> FilterResult:
        text = f"{c.title or ''} {c.snippet or ''}"
        mode = subtype.filter_mode or self.mode_by_taxonomy.get(taxonomy_lv2, "balanced")

        # hard-negative: mode 무관 항상 (광고/스팸/제휴/명백 무관)
        for hn in self.hard_negatives:
            if hn in text or hn in c.domain:
                return FilterResult("fail", f"hard_negative:{hn}")

        # negative_patterns: balanced/strict
        if mode in ("balanced", "strict"):
            for neg in subtype.negative_patterns:
                if neg in text:
                    return FilterResult("fail", f"negative_pattern:{neg}")

        # keyword 게이트: strict는 항상, balanced는 키워드 검색 경로(serpapi_site)만, minimal은 생략
        require_kw = mode == "strict" or (mode == "balanced" and c.discovery_method == "serpapi_site")
        if require_kw:
            terms = subtype.keywords + subtype.positive_patterns
            if terms and not any(t in text for t in terms):
                return FilterResult("fail", "no_keyword_match")

        # 한국 관련성: 항상
        known = self.registry.lookup(c.domain).site_name != "unknown"
        if not known and not _HANGUL.search(text):
            return FilterResult("fail", "not_korea_relevant")

        # 날짜: 힌트 있을 때만 (없으면 통과 — published_at 누락 허용)
        if c.published_at_hint and not self.in_range(c.published_at_hint):
            return FilterResult("fail", f"out_of_date_range:{c.published_at_hint}")

        return FilterResult("pass", None)

    def in_range(self, date_str: str) -> bool:
        after = self.date_range.get("after")
        before = self.date_range.get("before")
        if after and date_str < after:
            return False
        if before and date_str > before:
            return False
        return True
