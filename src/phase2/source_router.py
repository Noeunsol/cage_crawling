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

from src.common.sources.board import discover_dcinside_trend
from src.phase2.intent_builder import CollectionIntent
from src.phase2.provider import SearchResult, SerpApiProvider, TavilyProvider

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


def final_query(plan, source: dict, p2: dict, lv2: str) -> str:
    """provider에 실제로 나가는 검색식. UI 미리보기가 이걸 그대로 보여준다."""
    if source.get("method") == "board_list":
        return f"board:{', '.join(g.get('name') or g['id'] for g in source.get('galleries') or [])}"
    if source.get("method") == "web_search":
        return plan.query
    domains = source.get("domains") or ([source["domain"]] if source.get("domain") else [])
    suffix_map = (p2.get("serpapi", {}).get("rules_by_lv2", {}).get(lv2, {})
                  .get("query_suffixes_by_domain", {}))
    if len(domains) > 1:   # _serpapi가 append_terms로 붙이는 형태 그대로
        merged = list(dict.fromkeys(s for d in domains for s in suffix_map.get(d, [])))
        grouped = f"{plan.query} ({' OR '.join('site:' + d for d in domains)})"
        return " ".join([grouped, *merged])
    domain = domains[0] if domains else ""
    suffixes = suffix_map.get(domain, [])
    return " ".join([f"site:{domain}" if domain else "", plan.query, *suffixes]).strip()


# 검색어 1개가 몇 번의 SerpAPI 호출(=크레딧)이 되는지는 여기서 갈린다.
#   site_or_group(기본) : 도메인들을 site: OR 한 줄로 묶어 1회 호출
#   site_per_query      : 도메인마다 따로 호출 → 크레딧이 도메인 수만큼 는다
_SITE_PER_QUERY = "site_per_query"


def serpapi_rule(source: dict, strategy: dict, p2: dict, lv2: str) -> dict:
    """SerpAPI에 실제로 나갈 검색 규칙. 실행과 UI 미리보기가 이 함수를 함께 쓴다.

    serpapi.rules_by_lv2에 적은 값을 그대로 물려준다 — 설정해 두고 안 먹는 상태를 만들지 않는다.
    다만 검색 대상 도메인의 기본값은 전략의 source이고, rules_by_lv2의 domains_by_type이
    있으면 type별로 그쪽이 이긴다(provider._domains).
    """
    existing = p2.get("serpapi", {}).get("rules_by_lv2", {}).get(lv2, {})
    domains = source.get("domains") or ([source["domain"]] if source.get("domain") else [])
    rule = {
        "domains": domains,
        "max_domains_per_query": 1,
        "query_suffixes_by_domain": dict(existing.get("query_suffixes_by_domain", {})),
        # 기간은 전략의 recency_days가 정본이고, config에 tbs를 적었으면 그쪽이 이긴다.
        "tbs": existing.get("tbs") or _recency_tbs(strategy.get("recency_days")),
    }
    for key in ("domains_by_type", "keyword_groups_by_type", "append_terms", "append_terms_by_type"):
        if existing.get(key):
            rule[key] = existing[key]
    # type별 도메인을 지정했거나 site_per_query를 골랐으면 도메인마다 따로 호출한다.
    if existing.get("domains_by_type") or existing.get("query_strategy") == _SITE_PER_QUERY:
        rule["max_domains_per_query"] = int(existing.get("max_domains_per_query", 1))
        return rule
    if len(domains) > 1:
        # 매체별로 따로 검색하면 검색어 1개가 매체 수만큼 크레딧을 먹는다(뉴스 5개 소스 = 5배).
        # site: OR 한 줄이면 1크레딧이고 결과 수도 같다. final_query 미리보기와도 일치한다.
        rule["domains"] = []
        rule["append_terms"] = [f"site:{d}" for d in domains]
        # 묶어 보내면 domain이 None이라 도메인별 suffix가 안 잡힌다. 대상 도메인들의
        # 제외 연산자를 합쳐 "*"로 넘긴다(-inurl:tag 같은 건 AND로 붙어야 해서
        # append_terms(OR 그룹)에 넣으면 안 된다).
        merged = [suffix for domain in domains
                  for suffix in rule["query_suffixes_by_domain"].get(domain, [])]
        if merged:
            rule["query_suffixes_by_domain"] = {"*": list(dict.fromkeys(merged))}
    return rule


def _serpapi(plans, source, strategy, lv2, ctx) -> list[SearchResult]:
    options = ctx.p2.get("serpapi", {}).get("provider", {})
    rule = serpapi_rule(source, strategy, ctx.p2, lv2)
    factory = ctx.serpapi_factory or SerpApiProvider
    provider = factory(options, {lv2: rule})
    results = provider.search(_intent_for(plans, strategy, lv2, int(options.get("num", 10))))
    ctx.usage.extend(provider.usage_summary()["queries"])
    return results


def tavily_options(source: dict, strategy: dict, p2: dict, today, require_news: bool = False) -> dict:
    """Tavily에 실제로 나갈 요청 옵션. 실행과 UI 미리보기가 이 함수를 함께 쓴다."""
    options = dict(p2.get("providers", {}).get("tavily", {}))
    # snippet으로 fetch를 거르는 LV2만 비싼 advanced를 산다. 나머지는 본문을 읽는
    # acceptance가 판정하므로 basic으로 충분하다(실측 12.0 vs 8.2 URL/credit).
    if strategy.get("search_depth"):
        options["search_depth"] = strategy["search_depth"]
        options["chunks_per_source"] = int(
            strategy.get("chunks_per_source",
                         5 if strategy["search_depth"] == "advanced" else 1))
    # topic=news면 Tavily가 뉴스 색인만 본다. 두 가지가 한꺼번에 해결된다.
    #  - 법률상담·로펌 SEO 페이지가 구조적으로 빠진다(실측 2026-08-20: 1_A web_search
    #    후보 33건 중 15건이 lawtalk·대륜·법무법인 류였고 도메인 꼬리가 끝없었다)
    #  - start_date가 발행일 기준으로 실제 작동한다(general에서는 색인 시점이라 무력했고,
    #    365일을 걸었는데 저장 97건 중 65건이 1,000일 초과였다)
    topic = source.get("tavily_topic") or strategy.get("tavily_topic")
    if require_news:
        topic = "news"
    if topic:
        options["topic"] = topic
    # 뉴스 색인은 영어권 위주다. country는 가중치일 뿐이라 화이트리스트로 국내 매체만 본다.
    include = source.get("include_domains") or strategy.get("include_domains")
    if not include and options.get("topic") == "news":
        include = options.get("korean_news_domains")
    if include:
        options["include_domains"] = list(include)
    days = strategy.get("recency_days")
    if days:
        options["start_date"] = (today - timedelta(days=int(days))).isoformat()
        options.pop("time_range", None)     # start_date와 함께 쓰면 더 좁은 쪽이 이긴다
    return options


def _tavily(plans, source, strategy, lv2, ctx) -> list[SearchResult]:
    options = tavily_options(source, strategy, ctx.p2, ctx.today,
                             require_news=_requires_news_source(ctx, source))
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
        galleries, ctx.registry, ctx.fetcher, max_pages=int(source.get("max_pages", 1)),
        request_delay=float(source.get("request_delay", 2.0)))
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


def _requires_news_source(ctx, source) -> bool:
    accept = dict((ctx.p2.get("default_acceptance") or {}))
    accept.update(((source.get("overrides") or {}).get("acceptance") or {}))
    return bool(accept.get("require_news_source"))


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
    if _requires_news_source(ctx, source):
        news_domains = set(ctx.p2.get("providers", {}).get("tavily", {}).get("korean_news_domains") or [])
        domains = set(source.get("domains") or ([source["domain"]] if source.get("domain") else []))
        if method == "board_list" or (method == "serpapi_site" and not (domains & news_domains)):
            log.info("source %s skipped because require_news_source is enabled", source.get("id"))
            return []
    return _METHODS[method](plans, source, strategy, lv2, ctx)
