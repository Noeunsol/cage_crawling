"""tavily — subtype 설명/자연어 쿼리로 의미 검색 (Tavily)."""
from __future__ import annotations

from .base import search_discover


def discover(task, subtype, registry, query_gen, budget, max_urls) -> list:
    return search_discover("tavily", "tavily", task, subtype, registry,
                           query_gen, budget, max_urls)
