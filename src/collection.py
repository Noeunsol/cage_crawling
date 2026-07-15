"""Collection Strategy Router — 키워드-only의 한계를 보완하는 하이브리드 수집.

키워드 검색만으로는 은어·우회표현·맥락 사례를 놓치므로 여러 경로를 병렬로 둔다.
각 후보에 collection_method를 태그해 리포트에서 경로별 성능을 비교할 수 있다.

구현(mock):
  keyword       — 기존 키워드 OR 검색 (SerpAPI)
  semantic      — subtype 설명/자연어 쿼리로 의미 검색 (Tavily)
  site_sampling — 위험 가능성 높은 게시판의 인기글을 키워드 없이 샘플링 → 이후 matcher가 분류
미구현(seam, 실제 인프라 필요):
  seed_expansion — 고신뢰 문서 주변 링크 확장 (추출 후 링크 필요)
  trend          — 인기글에서 신조어 추출 (LLM/통계 + 사람 검토)
"""
from __future__ import annotations

import hashlib
import logging

from .policy import Subtype
from .query import GeneratedQuery, QueryGenerator
from .schema import UrlCandidate
from .search import get_client, run_client
from .site_registry import SiteRegistry

log = logging.getLogger(__name__)


class Strategy:
    name = "base"

    def collect(self, taxonomy_lv2: str, subtype: Subtype) -> list[UrlCandidate]:
        raise NotImplementedError


class KeywordStrategy(Strategy):
    """A. 키워드 기반 — priority_sites에 site 제한 키워드 검색. primary→fallback API."""
    name = "keyword"

    def __init__(self, registry: SiteRegistry, query_gen: QueryGenerator):
        self.registry = registry
        self.query_gen = query_gen

    def collect(self, taxonomy_lv2, subtype):
        for api in subtype.search_apis:
            client = get_client(api)
            if client is None:
                continue  # Exa 등 미구현 → 다음 fallback
            queries: list[GeneratedQuery] = []
            for site_name in subtype.priority_sites or [None]:
                queries += self.query_gen.generate(subtype, site_name, client.name)
            results = run_client(client, queries, taxonomy_lv2, subtype, self.registry, "keyword")
            if results:
                return results
        return []


class SemanticStrategy(Strategy):
    """B. 의미 기반 — 키워드 대신 subtype 설명/semantic_queries를 자연어로 검색 (Tavily)."""
    name = "semantic"

    def __init__(self, registry: SiteRegistry):
        self.registry = registry

    def collect(self, taxonomy_lv2, subtype):
        client = get_client("tavily")  # 의미 검색은 Tavily/Exa가 적합, Exa 미구현이라 Tavily
        if client is None:
            return []
        texts = subtype.semantic_queries or [subtype.description]
        queries = [GeneratedQuery(t, "semantic", "tavily", None) for t in texts if t]
        return run_client(client, queries, taxonomy_lv2, subtype, self.registry, "semantic")


class SiteSamplingStrategy(Strategy):
    """E. 사이트/게시판 샘플링 — 키워드 없이 인기글을 뽑아 이후 matcher가 분류(노이즈 보완용).

    seed_boards가 있으면 그걸, 없으면 priority_sites를 사용. 대부분 오탐이라 matcher에서
    걸러지지만, 키워드 사전에 없는 사례를 잡는 게 목적이다.
    """
    name = "site_sampling"
    samples_per_board = 3

    def __init__(self, registry: SiteRegistry):
        self.registry = registry

    def collect(self, taxonomy_lv2, subtype):
        boards = subtype.seed_boards or [{"site": s, "board": "best", "mode": "popular"}
                                         for s in subtype.priority_sites]
        kw_phrase = " ".join(subtype.keywords[:3]) if subtype.keywords else subtype.name
        out: list[UrlCandidate] = []
        for board in boards:
            domain = self.registry.domain_for(board.get("site", ""))
            if not domain:
                continue
            for i in range(self.samples_per_board):
                h = hashlib.sha256(
                    f"sampling|{domain}|{board.get('board')}|{subtype.name}|{i}".encode()
                ).hexdigest()
                # 1/3 만 실제 주제 글(스니펫에 키워드), 나머지는 일반 인기글(노이즈)
                topical = (i % 3 == 0)
                snippet = (f"베스트글: {kw_phrase} 관련 반응과 댓글..." if topical
                           else "오늘의 인기글 모음, 다양한 주제의 게시글과 댓글...")
                out.append(UrlCandidate(
                    source_url=f"https://{domain}/{board.get('board','best')}/{h[:10]}",
                    domain=domain,
                    search_query=f"[sampling] {board.get('site')}/{board.get('board')} {board.get('mode')}",
                    search_api="none",
                    taxonomy_lv2_candidate=taxonomy_lv2,
                    subtype_candidate=subtype.name,
                    title=f"[인기글] {board.get('site')} {h[:4]}",
                    snippet=snippet,
                    published_at_hint="2026-06-12" if int(h[:2], 16) % 2 == 0 else None,
                    score=0.3,
                    collection_method="site_sampling",
                ))
        return out


# 미구현 전략: enable돼도 경고만 남기고 빈 결과 (seam)
_UNIMPLEMENTED = {"seed_expansion", "trend"}


class CollectionStrategyRouter:
    def __init__(self, enabled: list[str], registry: SiteRegistry, query_gen: QueryGenerator):
        self.enabled = enabled
        self._strategies = {
            "keyword": KeywordStrategy(registry, query_gen),
            "semantic": SemanticStrategy(registry),
            "site_sampling": SiteSamplingStrategy(registry),
        }

    def collect(self, taxonomy_lv2: str, subtype: Subtype) -> list[UrlCandidate]:
        out: list[UrlCandidate] = []
        for name in self.enabled:
            strat = self._strategies.get(name)
            if strat is None:
                if name in _UNIMPLEMENTED:
                    log.info("collection strategy '%s' 미구현 (seam) → skip", name)
                else:
                    log.warning("알 수 없는 collection strategy '%s' → skip", name)
                continue
            out.extend(strat.collect(taxonomy_lv2, subtype))
        return out
