"""DiscoveryRouter — task의 discovery method만 실행해 공통 UrlCandidate 목록으로 만든다.

method 이름 → 해당 discovery 모듈로 dispatch. provider 하나가 실패해도 전체 run은 계속한다.
Budget이 method당 쿼리 예산을 소진하며, 소진되면 이후 method는 건너뛴다.
"""
from __future__ import annotations

import logging

from ..fetcher import Fetcher
from . import board, exa, rss, seed, serpapi, sitemap, tavily

log = logging.getLogger(__name__)


class DiscoveryRouter:
    def __init__(self, registry, query_gen, budget, max_urls_per_query: int = 5,
                 settings: dict | None = None):
        self.registry, self.query_gen, self.budget = registry, query_gen, budget
        self.max_urls = max_urls_per_query
        self.fetcher = Fetcher(settings)

    def discover(self, task, subtype) -> list:
        out = []
        for method in task.discovery_methods:
            try:
                found = self._run(method, task, subtype)
            except Exception as exc:  # provider 하나가 전체 run을 멈추지 않음
                log.warning("discovery 실패 method=%s subtype=%s: %s", method, task.subtype, exc)
                continue
            out.extend(found[:self.max_urls])
        return out

    def _run(self, method: str, task, subtype) -> list:
        if method == "board_list":
            return board.discover(task, subtype, self.registry, self.fetcher)
        if method == "rss":
            return rss.discover(task, subtype, self.registry)
        if method == "sitemap":
            return sitemap.discover(task, subtype, self.registry)
        if method == "seed_url":
            return seed.discover(task, subtype, self.registry)
        if method == "serpapi_site":
            return serpapi.discover(task, subtype, self.registry, self.query_gen,
                                    self.budget, self.max_urls)
        if method == "tavily":
            return tavily.discover(task, subtype, self.registry, self.query_gen,
                                   self.budget, self.max_urls)
        if method in {"exa", "github"}:
            return exa.discover(method, task, subtype, self.registry, self.query_gen,
                                self.budget, self.max_urls)
        log.warning("알 수 없는 discovery method '%s'", method)
        return []
