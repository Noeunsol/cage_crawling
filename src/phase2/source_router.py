"""검색 계획 → 기존 discovery 구현 호출. source의 method 분기 하나가 전부다.

provider마다 새 파이프라인을 만들지 않는다. 반환 타입을 SearchResult로 통일해
기존 rerank → to_candidate → extract → acceptance 경로를 그대로 재사용한다.

access 규약
  direct        : 검색하고 본문도 수집한다
  discovery_only: 검색 결과(메타)만 쓰고 본문은 수집하지 않는다 (gap_filling이 fetch를 막는다)
  metadata_only : 제목·날짜·URL만 쓴다
  blocked       : 호출조차 하지 않는다 (robots 차단 등)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..discovery.board import discover_dcinside_trend
from .intent_builder import CollectionIntent
from .provider import SearchResult, SerpApiProvider, TavilyProvider

log = logging.getLogger(__name__)


@dataclass
class RouterContext:
    """source_router가 기존 구현을 호출하는 데 필요한 것만 담는다."""
    p2: dict
    registry: object
    fetcher: object
    today: date = field(default_factory=date.today)
    tavily_factory: object = None       # 테스트에서 provider를 갈아끼우기 위한 seam
    serpapi_factory: object = None
    usage: list = field(default_factory=list)

    def usage_summary(self) -> dict:
        return {"calls": len(self.usage),
                "credits": round(sum(e.get("credits", 0) for e in self.usage), 3),
                "queries": list(self.usage)}


def _recency_tbs(days: int | None) -> str | None:
    """SerpAPI tbs. days를 덮는 가장 좁은 구간을 고른다."""
    if not days:
        return None
    for limit, value in ((1, "qdr:d"), (7, "qdr:w"), (31, "qdr:m"), (366, "qdr:y")):
        if days <= limit:
            return value
    return None  # 1년 초과는 Google 기본(제한 없음)


def _intent_for(plans, strategy: dict, lv2: str, max_results: int) -> CollectionIntent:
    """rerank와 provider가 기대하는 최소 intent. 검색어는 plan 그대로 나간다."""
    include_by_type = strategy.get("include_by_type") or {}
    return CollectionIntent(
        target_taxonomy_lv2=lv2,
        queries=[p.query for p in plans],
        query_types={p.query: p.target_type for p in plans},
        include_by_type=include_by_type,
        missing_types=[], dropped_queries=[],
        include=sorted({term for p in plans for term in p.expected_lv2_evidence}),
        exclude=list(strategy.get("exclude") or []),
        event_terms=list(strategy.get("event_terms") or []),
        korea_relevance_requirement="한국 사건·피해·대응 맥락",
        excluded_domains=list(strategy.get("excluded_domains") or []),
        max_results=max_results,
        max_searches=len(plans),
    )


def _serpapi(plans, source, strategy, lv2, ctx) -> list[SearchResult]:
    options = ctx.p2.get("serpapi", {}).get("provider", {})
    existing = ctx.p2.get("serpapi", {}).get("rules_by_lv2", {}).get(lv2, {})
    # 계획된 검색어를 그대로 쓴다. keyword_groups_by_type은 의도적으로 물려주지 않는다.
    rule = {
        "domains": [source["domain"]] if source.get("domain") else [],
        "max_domains_per_query": 1,
        "query_suffixes_by_domain": existing.get("query_suffixes_by_domain", {}),
        "tbs": _recency_tbs(strategy.get("recency_days")),
    }
    factory = ctx.serpapi_factory or SerpApiProvider
    provider = factory(options, {lv2: rule})
    results = provider.search(_intent_for(plans, strategy, lv2, int(options.get("num", 10))))
    ctx.usage.extend(provider.usage_summary()["queries"])
    return results


def _tavily(plans, source, strategy, lv2, ctx) -> list[SearchResult]:
    options = dict(ctx.p2.get("providers", {}).get("tavily", {}))
    days = strategy.get("recency_days")
    if days:
        options["start_date"] = (ctx.today - timedelta(days=int(days))).isoformat()
        options.pop("time_range", None)     # start_date와 함께 쓰면 더 좁은 쪽이 이긴다
    factory = ctx.tavily_factory or TavilyProvider
    provider = factory(options)
    results = provider.search(
        _intent_for(plans, strategy, lv2, int(options.get("max_results_per_query", 10))))
    ctx.usage.extend(provider.usage_summary()["queries"])
    return results


def _board(plans, source, strategy, lv2, ctx) -> list[SearchResult]:
    """게시판은 검색어를 쓰지 않는다. 최신 목록을 그대로 후보로 삼는다."""
    galleries = source.get("galleries") or []
    if not galleries:
        return []
    candidates = discover_dcinside_trend(
        galleries, ctx.registry, ctx.fetcher, max_pages=int(source.get("max_pages", 1)))
    return [
        SearchResult(
            provider="board_list", target_taxonomy_lv2=lv2,
            query_or_intent=f"board:{c.meta.get('board_name', source['id'])}",
            query_type="", title=c.title or "", url=c.source_url,
            snippet=c.snippet, content_hint=c.snippet,
            published_at_hint=c.published_at_hint, provider_score=c.score, rank=rank,
            raw_provider_payload=c.meta,
        )
        for rank, c in enumerate(candidates, start=1)
    ]


_METHODS = {
    "serpapi_site": _serpapi,
    "official_board": _serpapi,
    "web_search": _tavily,
    "board_list": _board,
}


def discover(plans, source: dict, strategy: dict, lv2: str, ctx: RouterContext) -> list[SearchResult]:
    if source.get("access") == "blocked":
        log.info("source %s is blocked — skipped without an API call", source.get("id"))
        return []
    method = source.get("method", "")
    if method not in _METHODS:
        raise ValueError(f"unsupported method: {method}")
    if method != "board_list" and not plans:
        return []
    return _METHODS[method](plans, source, strategy, lv2, ctx)
