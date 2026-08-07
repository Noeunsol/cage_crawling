"""Phase 1 — Query Generator. taxonomy policy + site + API별 검색어 생성.

API별 스타일을 분리한다 (설계서 §1):
  - serpapi: Google syntax — site:, "정확구문", OR, after:, before:, 제외어(-)
  - tavily:  자연어 기반 검색어
Query 유형은 최소 3종 이상 생성 (정확/동의어확장/사이트제한/최근성/제외어).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..policy import Subtype
from ..site_registry import SiteRegistry


@dataclass
class GeneratedQuery:
    text: str
    query_type: str        # exact / site_restricted / recency / negative / natural
    search_api: str
    site_name: str | None  # 사이트 제한 쿼리면 site_name, 아니면 None


class QueryGenerator:
    def __init__(self, registry: SiteRegistry, date_range: dict | None = None):
        self.registry = registry
        self.date_range = date_range or {}

    def generate_for_task(self, subtype: Subtype, task, search_api: str) -> list[GeneratedQuery]:
        """Strategy별 검색 의도와 provider 문법을 반영한다."""
        signals = task.target_harm_signals or subtype.keywords
        context = " ".join(subtype.positive_patterns)
        original = subtype.keywords
        subtype.keywords = signals
        try:
            if search_api == "serpapi":
                sites = subtype.priority_sites or [None]
                queries = [q for site in sites for q in self._serpapi(subtype, site)]
            else:
                natural = f"{task.collection_type} 한국 사례 {context} {' '.join(signals)}"
                neg = " ".join(f"-{n}" for n in subtype.negative_patterns)
                queries = [GeneratedQuery(f"{natural} {neg}".strip(), "strategy", search_api, None)]
            return queries
        finally:
            subtype.keywords = original

    # ── SerpAPI: Google syntax ──
    def _serpapi(self, subtype: Subtype, site_name: str) -> list[GeneratedQuery]:
        kw = _or_group(subtype.keywords)
        domain = self.registry.domain_for(site_name)
        site_clause = f"site:{domain} " if domain else ""
        recency = self._recency_clause()
        neg = " ".join(f'-"{n}"' for n in subtype.negative_patterns)

        queries = [
            GeneratedQuery(kw, "exact", "serpapi", None),
            GeneratedQuery(f"{site_clause}{kw}".strip(), "site_restricted", "serpapi", site_name),
            GeneratedQuery(f"{site_clause}{kw} {recency}".strip(), "recency", "serpapi", site_name),
        ]
        if neg:
            queries.append(
                GeneratedQuery(f"{site_clause}{kw} {neg}".strip(), "negative", "serpapi", site_name)
            )
        return queries

    def _recency_clause(self) -> str:
        after = self.date_range.get("after")
        before = self.date_range.get("before")
        parts = []
        if after:
            parts.append(f"after:{after}")
        if before:
            parts.append(f"before:{before}")
        return " ".join(parts)


def _or_group(keywords: list[str]) -> str:
    """['악플','조리돌림'] → '("악플" OR "조리돌림")'."""
    if not keywords:
        return ""
    quoted = " OR ".join(f'"{k}"' for k in keywords)
    return f"({quoted})"
