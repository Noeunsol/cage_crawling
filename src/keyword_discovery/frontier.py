"""Phase 3 — URL Frontier. 중복 제거 / 우선순위 점수 / 상태 관리.

Search 결과를 바로 추출하지 않고 큐에 쌓아 dedup + priority 정렬 후 내보낸다.
Focused Crawl 이므로 여기서 페이지 링크를 확장(spider)하지 않는다.
"""
from __future__ import annotations

from ..schema import UrlCandidate
from ..site_registry import SiteRegistry

# 신뢰도 낮거나 위험한 소스는 감점 (예시)
_UNSAFE_HINT = ("bit.ly", "t.co")
_REFERENCE_DOMAINS = ("namu.wiki", "wikipedia.org", "wikiwand.com")
_REFERENCE_WORDS = ("개요", "정의", "뜻", "용어", "나무위키")


class UrlFrontier:
    def __init__(self, registry: SiteRegistry):
        self.registry = registry
        self._seen: set[str] = set()          # dedup용 canonical key
        self._domains_seen: dict[str, int] = {}  # source diversity용
        self.items: list[UrlCandidate] = []

    def add_many(self, candidates: list[UrlCandidate]) -> int:
        added = 0
        for c in candidates:
            if self.add(c):
                added += 1
        return added

    def add(self, c: UrlCandidate) -> bool:
        key = c.dedup_key()
        if key in self._seen:
            return False   # 중복 제거
        self._seen.add(key)
        info = self.registry.lookup(c.domain)
        c.canonical_url = key
        c.site_name, c.site_type = info.site_name, info.site_type
        text = f"{c.title or ''} {c.snippet or ''}"
        c.taxonomy_fit_url_score = 0.2 if text.strip() else 0.0
        c.harm_signal_url_score = min(sum(s in text for s in ("피해", "공격", "유출", "협박", "악플")) * 0.1, 0.3)
        c.source_priority_score = 0.2 if info.site_type in {"community", "qna", "news"} else 0.05
        c.reference_page_penalty = 0.6 if (any(d in c.domain for d in _REFERENCE_DOMAINS)
            or any(w in text for w in _REFERENCE_WORDS) or "/wiki/" in c.source_url or "/edit/" in c.source_url) else 0.0
        c.value_score = self._value(c)
        c.extraction_likelihood = self._likelihood(c)
        c.score = c.value_score                      # 정렬 = 가치 점수
        self._domains_seen[c.domain] = self._domains_seen.get(c.domain, 0) + 1
        self.items.append(c)
        return True

    def pending(self) -> list[UrlCandidate]:
        """priority 내림차순으로 pending 후보 반환."""
        return sorted(
            (c for c in self.items if c.status == "pending"),
            key=lambda c: c.score,
            reverse=True,
        )

    def _value(self, c: UrlCandidate) -> float:
        """value_score = search_rank + kw + site_priority + recency + raw_source - penalty.

        escalation(Firecrawl/Playwright) 게이트로도 쓰이므로 taxonomy raw 가치를 반영한다 (§6).
        """
        info = self.registry.lookup(c.domain)
        # raw_source_score: 커뮤니티(네이트판/디시/에펨)가 Toxic raw 가치 높음
        raw_source = {"community": 0.3, "qna": 0.25, "news": 0.15, "blog": 0.1}.get(info.site_type, 0.1)
        site_priority = {"qna": 0.15, "news": 0.15, "blog": 0.1, "community": 0.1}.get(info.site_type, 0.05)
        recency = 0.15 if c.published_at_hint else 0.0
        relevance = 0.15 if (c.title or c.snippet) else 0.0
        diversity = 0.1 / (1 + self._domains_seen.get(c.domain, 0))
        unsafe = -0.5 if any(h in c.domain for h in _UNSAFE_HINT) else 0.0
        low_context = -0.1 if not (c.snippet or "").strip() else 0.0
        ct_score = 0.15 if c.collection_type in {"raw_expression", "qa_consulting", "news_case", "technical_security"} else 0.05
        return round(c.score + raw_source + site_priority + recency + relevance + diversity
                     + c.taxonomy_fit_url_score + c.harm_signal_url_score + c.source_priority_score
                     + ct_score + unsafe + low_context - c.reference_page_penalty, 4)

    @staticmethod
    def is_reference(c: UrlCandidate) -> bool:
        return c.reference_page_penalty > 0

    def _likelihood(self, c: UrlCandidate) -> float:
        """정적 추출 성공 가능성 heuristic. community/dynamic(JS·차단) 낮음."""
        info = self.registry.lookup(c.domain)
        return {"qna": 0.8, "news": 0.85, "blog": 0.8, "tech": 0.8,
                "community": 0.3, "dynamic": 0.2}.get(info.site_type, 0.5)
