"""serpapi_site — 키워드 site 제한 검색 (SerpAPI, google syntax)."""
from __future__ import annotations

from .base import search_discover


def discover(task, subtype, registry, query_gen, budget, max_urls) -> list:
    return search_discover("serpapi", "serpapi_site", task, subtype, registry,
                           query_gen, budget, max_urls)
