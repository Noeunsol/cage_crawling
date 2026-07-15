"""Phase 3 — URL Frontier. 중복 제거 / 우선순위 점수 / 상태 관리.

Search 결과를 바로 추출하지 않고 큐에 쌓아 dedup + priority 정렬 후 내보낸다.
Focused Crawl 이므로 여기서 페이지 링크를 확장(spider)하지 않는다.
"""
from __future__ import annotations

from .schema import UrlCandidate
from .site_registry import SiteRegistry

# 신뢰도 낮거나 위험한 소스는 감점 (예시)
_UNSAFE_HINT = ("bit.ly", "t.co")


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
        c.score = self._priority(c)
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

    def _priority(self, c: UrlCandidate) -> float:
        """priority_score = keyword/site/recency/relevance/diversity - unsafe (설계서 §3)."""
        info = self.registry.lookup(c.domain)
        site_reliability = {"qna": 0.3, "news": 0.3, "blog": 0.2, "community": 0.15}.get(info.site_type, 0.1)
        recency = 0.2 if c.published_at_hint else 0.0
        relevance = 0.2 if _has_keyword_hint(c) else 0.0
        diversity = 0.1 / (1 + self._domains_seen.get(c.domain, 0))  # 같은 도메인 반복 감점
        unsafe = -0.5 if any(h in c.domain for h in _UNSAFE_HINT) else 0.0
        return round(c.score + site_reliability + recency + relevance + diversity + unsafe, 4)


def _has_keyword_hint(c: UrlCandidate) -> bool:
    text = f"{c.title or ''} {c.snippet or ''}"
    return bool(text.strip())
