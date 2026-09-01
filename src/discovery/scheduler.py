"""검색 요청을 언제·얼마나 보낼지 결정하는 스케줄러 (7절).

이 모듈은 discovery(검색 API 호출)까지만 담당한다. 찾은 URL을 실제로 가져와 저장하는 일은
fetcher/filter 단계(Phase 7~9)의 몫이라, 여기서는 후보 URL을 메모리 상에서 모아 돌려준다.
UI(session_state)에는 의존하지 않는다 — 날짜/비율은 이미 계산된 값으로 받는다.
"""

from __future__ import annotations

import math
import random
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import date

from src.discovery import adaptive_multiplier, scoring
from src.discovery.allocator import has_serpapi_domains, serpapi_allowed_domains, tavily_exclude_domains
from src.discovery.base import build_fingerprint
from src.query.repository import list_active_queries
from src.storage.repositories import domain_bundles as bundles_repo
from src.storage.repositories import queries as queries_repo
from src.storage.repositories import query_executions as exec_repo
from src.utils.quota import classify as classify_quota_error
from src.utils.rate_limit import throttle
from src.utils.urls import normalize_url


@dataclass
class ScheduledCandidate:
    lv2_id: str
    type_name: str
    provider: str
    query_id: int
    url: str
    rank: int
    relevance_score: float | None = None


@dataclass
class SchedulerResult:
    candidates: list[ScheduledCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provider_usage: dict = field(default_factory=dict)  # provider -> [usage dict, ...]


_MIN_YIELD_SAMPLES = 10  # 이보다 적으면 실측 대신 max_results_per_request(설정 최대치)를 그대로 씀


def _avg_result_count(conn, provider: str, providers_cfg: dict) -> float:
    """provider가 콜 한 번에 실제로 몇 건을 돌려주는지 실측 평균 (콜당 설정 최대치가 아니라 실제 수확량).

    tavily는 max_results_per_request가 20이지만 topic/date 필터링 탓에 실제로는 훨씬 적게 돌아오는 경우가
    많다 — 설정 최대치로 provider 간 효율을 가정하면 틀리기 쉬워서 query_executions 실측치를 우선 쓴다.
    """
    fallback = providers_cfg.get(provider, {}).get("max_results_per_request", 20 if provider == "tavily" else 10)
    row = conn.execute(
        """
        SELECT AVG(qe.result_count) avg_n, COUNT(*) n FROM query_executions qe
        JOIN search_queries sq ON sq.id = qe.query_id
        WHERE sq.provider = ? AND qe.status = 'success'
        """,
        (provider,),
    ).fetchone()
    if row["n"] and row["n"] >= _MIN_YIELD_SAMPLES and row["avg_n"]:
        return row["avg_n"]
    return fallback


def _call_weighted_ratio(conn, ratio: dict, providers_cfg: dict) -> dict:
    """provider_ratio(%)는 accepted 콘텐츠 배분 비율인데, provider마다 콜당 실제 수확량이 다르면 같은
    ratio%라도 실제 API 호출 수는 다르게 나온다. "ratio가 50:50이면 실제 호출 수도 50:50에 가깝게" 되도록
    콜당 실측 평균 수확량으로 가중해 보정한다."""
    tavily_weight = ratio["tavily"] * _avg_result_count(conn, "tavily", providers_cfg)
    serpapi_weight = ratio["serpapi"] * _avg_result_count(conn, "serpapi", providers_cfg)
    total_weight = tavily_weight + serpapi_weight
    if total_weight == 0:
        return {"tavily": 0, "serpapi": 0}
    return {
        "tavily": tavily_weight / total_weight * 100,
        "serpapi": serpapi_weight / total_weight * 100,
    }


@dataclass
class _Lane:
    """(type, provider) 하나의 진행 상태. round-robin이 도는 최소 단위 (7.5절)."""

    lv2_id: str
    type_name: str
    provider: str
    target: int
    date_from: date
    date_to: date
    search_kwargs: dict            # provider.search()에 넘길 고정 파라미터 (tavily의 exclude_domains 등).
                                    # serpapi는 allowed_domains를 여기 안 넣는다 — 매 호출마다 LRU로 채운다.
    queries: list

    collected: int = 0
    current_query: object | None = None
    page_start: int = 0
    current_domains: list[str] | None = None
    seen_urls: set[str] = field(default_factory=set)

    @property
    def done(self) -> bool:
        return self.collected >= self.target or (not self.queries and self.current_query is None)


def _build_lanes(
    conn, configs, targets, *, target_count, candidate_multiplier,
    date_range_by_lv2, provider_ratio_by_lv2, use_adaptive_multiplier=False,
    adaptive_multiplier_snapshot=None,
) -> tuple[list[_Lane], list[str]]:
    type_domains_cfg = configs["type_domains"]["types"]
    alias_groups = configs["domain_aliases"]["groups"]
    blacklist_domains = configs["blacklist"]["domains"]
    default_ratio = configs["collection"]["provider_ratio"]["default"]
    exploration_epsilon = configs["collection"].get("query_domain_exploration", {}).get(
        "epsilon", scoring.DEFAULT_EXPLORATION_EPSILON,
    )
    taxonomy_lookup = {
        (g["lv2_id"], t["name"]): t
        for g in configs["taxonomy"]["taxonomy"] for t in g["types"]
    }
    partition_cfg = configs["collection"].get("domain_partition", {})
    # 기본 True — config 키가 실수로 빠져도 실측 근거로 채택된 "provider 분리" 동작을 유지한다
    # (src/discovery/allocator.py 모듈 docstring 참고).
    exclude_serpapi_domains = partition_cfg.get("tavily_excludes_serpapi_domains", True)
    # target_count는 LV2 기준 목표다 — 같은 LV2에 type이 여럿이면 나눠 갖는다 (나머지는 올림).
    types_per_lv2 = Counter(lv2_id for lv2_id, _ in targets)

    lanes: list[_Lane] = []
    warnings: list[str] = []

    for lv2_id, type_name in targets:
        date_from, date_to = date_range_by_lv2[lv2_id]
        ratio = _call_weighted_ratio(
            conn, provider_ratio_by_lv2.get(lv2_id, default_ratio), configs.get("providers", {}),
        )
        per_type_target_count = math.ceil(target_count / types_per_lv2[lv2_id])

        config_has_domains = has_serpapi_domains(type_domains_cfg, type_name, blacklist_domains, lv2_id)
        bundle_key = f"{lv2_id}::{type_name}"
        if config_has_domains:
            # 도메인을 최대 3개씩 번들로 묶고(alias는 항상 같은 묶음), 상태를 DB에 맞춰둔다.
            bundles_repo.sync_bundles(
                conn, bundle_key,
                serpapi_allowed_domains(type_domains_cfg, type_name, blacklist_domains, lv2_id), alias_groups,
            )
        serpapi_available = config_has_domains and bundles_repo.has_enabled_bundle(conn, bundle_key)

        if use_adaptive_multiplier:
            # provider별로 실측 생존율 기반 multiplier를 따로 적용한다 (7절 확장, 2026-08-31).
            # candidate_multiplier(단일 float) 인자는 이 모드에서는 쓰지 않는다.
            if serpapi_available:
                tavily_accepted_share = round(per_type_target_count * ratio["tavily"] / 100)
                serpapi_accepted_share = per_type_target_count - tavily_accepted_share
            else:
                tavily_accepted_share, serpapi_accepted_share = per_type_target_count, 0
                reason = "SerpAPI 허용 도메인이 없어" if not config_has_domains else "모든 도메인 번들이 비활성화돼 있어"
                warnings.append(
                    f"{lv2_id}::{type_name}: {reason} 목표 {per_type_target_count}건 전량을 Tavily로 진행합니다."
                )
            tavily_target = (
                math.ceil(tavily_accepted_share * (adaptive_multiplier_snapshot or {}).get(
                    f"{lv2_id}::{type_name}::tavily",
                    adaptive_multiplier.compute_multiplier(conn, configs, lv2_id, type_name, "tavily"),
                )) if tavily_accepted_share > 0 else 0
            )
            serpapi_target = (
                math.ceil(serpapi_accepted_share * (adaptive_multiplier_snapshot or {}).get(
                    f"{lv2_id}::{type_name}::serpapi",
                    adaptive_multiplier.compute_multiplier(conn, configs, lv2_id, type_name, "serpapi"),
                )) if serpapi_accepted_share > 0 else 0
            )
        else:
            total = math.ceil(per_type_target_count * candidate_multiplier)
            if serpapi_available:
                tavily_target = round(total * ratio["tavily"] / 100)
                serpapi_target = total - tavily_target
            else:
                # 6.3절: 도메인이 없거나(config) 번들이 전부 비활성화된 type은 전량 Tavily로 이관한다.
                tavily_target, serpapi_target = total, 0
                reason = "SerpAPI 허용 도메인이 없어" if not config_has_domains else "모든 도메인 번들이 비활성화돼 있어"
                warnings.append(f"{lv2_id}::{type_name}: {reason} 목표 {total}건 전량을 Tavily로 진행합니다.")

        type_cfg = taxonomy_lookup.get((lv2_id, type_name), {})
        tavily_topic = type_cfg.get("tavily", {}).get("topic")
        if tavily_topic is None:
            tavily_topic = "general"
            warnings.append(f"{lv2_id}::{type_name}: tavily.topic 설정이 없어 기본값 general을 씁니다 (topic_config_missing).")

        provider_targets = [
            ("tavily", tavily_target, {
                "topic": tavily_topic,
                "exclude_domains": tavily_exclude_domains(
                    type_domains_cfg, blacklist_domains, type_name,
                    exclude_serpapi_domains=exclude_serpapi_domains,
                    lv2_id=lv2_id,
                ),
            }),
            # allowed_domains는 여기서 고정하지 않는다 — 매 검색 호출마다 LRU로 번들을 골라 채운다.
            ("serpapi", serpapi_target, {}),
        ]
        for provider, provider_target, search_kwargs in provider_targets:
            if provider_target <= 0:
                continue
            active_queries = list_active_queries(
                conn, taxonomy_lv2=lv2_id, type_name=type_name, provider=provider
            )
            if not active_queries:
                warnings.append(
                    f"{lv2_id}::{type_name} ({provider}): 사용할 검색어가 없어 이 provider는 건너뜁니다."
                )
                continue
            lanes.append(_Lane(
                lv2_id=lv2_id, type_name=type_name, provider=provider, target=provider_target,
                date_from=date_from, date_to=date_to, search_kwargs=search_kwargs,
                # 실측 성과가 좋은 쿼리를 먼저 시도하고, 반복적으로 0건/실패만 낸 쿼리는 뒤로
                # 미룬다(제외는 아님 — 7절 확장, 2026-08-31).
                queries=scoring.sort_queries_by_score(conn, active_queries, epsilon=exploration_epsilon),
            ))

    # target을 못 채워도 lane들이 무한정 검색어·페이지를 소진하지 않도록, (lv2, provider) 하나가
    # 함께 쓸 수 있는 콜 예산을 정해둔다 — lane(=type) 단위로 각자 예산을 주면 type 수만큼 곱해져서
    # 배수가 의도보다 훨씬 커진다(2026-08-31 리뷰에서 발견: type 5개 x lane당 3콜 = 15콜로 총
    # 기대치의 7배가 나왔다). "콜당 최대치를 다 채운다는 이상적인 가정으로 (lv2,provider) 전체가
    # 필요한 콜 수" x lane_call_budget_multiplier를 그 (lv2,provider)의 모든 type이 나눠 쓴다.
    budget_multiplier = configs.get("collection", {}).get("scheduling", {}).get("lane_call_budget_multiplier", 3)
    group_targets: dict[tuple[str, str], int] = {}
    for lane in lanes:
        group_targets[(lane.lv2_id, lane.provider)] = group_targets.get((lane.lv2_id, lane.provider), 0) + lane.target
    group_budgets = {}
    for (group_lv2, provider), total_target in group_targets.items():
        page_size = configs.get("providers", {}).get(provider, {}).get(
            "max_results_per_request", 20 if provider == "tavily" else 10,
        )
        group_budgets[(group_lv2, provider)] = max(1, math.ceil(total_target / page_size)) * budget_multiplier

    # 예산이 type 수보다 작으면 누군가는 이번 실행에서 콜을 못 받는다 — 그 자체는 어쩔 수 없지만
    # (콜 늘리면 비용 증가), round-robin이 매번 taxonomy.yaml 순서(cyberbullying -> ... ->
    # threats_and_intimidation)대로 시작하면 예산이 부족할 때 항상 같은 type(맨 뒤)만 열외된다.
    # lane 순서를 실행마다 섞어서 "열외되는 type"이 매번 랜덤하게 바뀌게 한다 (2026-09-01).
    # 전용 Random 인스턴스를 쓴다 — 전역 random을 쓰면 scoring.py의 epsilon-greedy 탐색이
    # 소비하는 난수 시퀀스가 밀려서 그쪽 테스트가 이 셔플 유무에 따라 흔들린다.
    random.Random().shuffle(lanes)

    return lanes, warnings, group_budgets


def run_scheduler(
    conn,
    providers: dict,   # {"tavily": TavilyProvider, "serpapi": SerpApiProvider}
    configs: dict,
    run_id: str,
    targets: list[tuple[str, str]],   # [(lv2_id, type_name), ...] — 활성화된 type들
    *,
    target_count: int,
    candidate_multiplier: float,
    date_range_by_lv2: dict[str, tuple[date, date]],
    provider_ratio_by_lv2: dict[str, dict],
    max_calls_by_provider: dict[str, int] | None = None,
    use_adaptive_multiplier: bool = False,
    adaptive_multiplier_snapshot: dict[str, float] | None = None,
) -> SchedulerResult:
    """활성 type이 고르게 검색되도록 round-robin으로 provider를 호출한다 (7.4, 7.5절).

    - 새 요청을 시작하기 직전에만 목표 달성 여부를 확인한다. 이미 시작한 요청은 끝까지 처리한다.
    - 동일 조건(같은 fingerprint)으로 이미 검색한 적이 있으면 API를 다시 부르지 않는다 (10.1절).
    - max_calls_by_provider가 있으면 provider별 실제 API 호출 수(캐시 적중 제외)에 상한을 건다
      (실험용 — target_count 계산과 무관하게 이번 실행만 강제로 줄인다).
    - use_adaptive_multiplier=True면 candidate_multiplier(단일 값) 대신 provider별 실측 생존율
      기반 multiplier를 쓴다 (adaptive_multiplier.py, 2026-08-31). 기본은 False — 기존 UI/실행
      경로는 지금까지와 완전히 동일하게 동작한다.
    """
    lanes, warnings, group_budgets = _build_lanes(
        conn, configs, targets, target_count=target_count, candidate_multiplier=candidate_multiplier,
        date_range_by_lv2=date_range_by_lv2, provider_ratio_by_lv2=provider_ratio_by_lv2,
        use_adaptive_multiplier=use_adaptive_multiplier,
        adaptive_multiplier_snapshot=adaptive_multiplier_snapshot,
    )
    result = SchedulerResult(warnings=warnings)
    queue = deque(lane for lane in lanes if not lane.done)
    calls_used = Counter()
    group_calls_used: Counter = Counter()  # (lv2_id, provider) -> 실제 호출 수 (그 lv2의 모든 type이 공유)
    exploration_epsilon = configs["collection"].get("query_domain_exploration", {}).get(
        "epsilon", scoring.DEFAULT_EXPLORATION_EPSILON,
    )
    capped_providers: set[str] = set()
    capped_groups: set[tuple[str, str]] = set()

    def _is_capped(provider: str) -> bool:
        if provider in capped_providers:
            return True
        if not max_calls_by_provider or provider not in max_calls_by_provider:
            return False
        return calls_used[provider] >= max_calls_by_provider[provider]

    def _is_group_capped(lv2_id: str, provider: str) -> bool:
        group = (lv2_id, provider)
        if group in capped_groups:
            return True
        return group_calls_used[group] >= group_budgets.get(group, math.inf)

    while queue:
        lane = queue.popleft()
        if _is_capped(lane.provider):
            if lane.provider not in capped_providers:
                capped_providers.add(lane.provider)
                result.warnings.append(
                    f"{lane.provider}: 지정한 최대 호출 수({max_calls_by_provider[lane.provider]}건)에 "
                    "도달해 남은 검색을 건너뜁니다."
                )
            continue  # 이 lane은 더 진행할 수 없다 — 큐에 다시 넣지 않는다.
        if _is_group_capped(lane.lv2_id, lane.provider):
            group = (lane.lv2_id, lane.provider)
            if group not in capped_groups:
                capped_groups.add(group)
                result.warnings.append(
                    f"{lane.lv2_id} ({lane.provider}): 콜 예산({group_budgets[group]}건)을 다 써서 "
                    "목표 미달 상태로 포기합니다 — API 낭비를 막기 위한 안전장치입니다."
                )
            continue  # 이 (lv2, provider) 예산이 바닥났다 — 큐에 다시 넣지 않는다.
        if lane.current_query is None:
            lane.current_query = lane.queries.pop(0)
        query_row = lane.current_query

        search_kwargs = dict(lane.search_kwargs)
        # 같은 검색어라도 taxonomy/type이 다르면 별도 실험으로 취급한다.
        fingerprint_extra = {"taxonomy_lv2": lane.lv2_id, "type_name": lane.type_name}
        if lane.provider == "tavily" and search_kwargs.get("topic") != "general":
            # "general"은 topic 파라미터가 생기기 전의 암묵적 기본값과 동일하므로 지문에서 뺀다 —
            # 그래야 이 기능 이전에 쌓인 request_fingerprint 캐시가 계속 유효하다. "news"처럼
            # 실제로 다른 걸 요청하는 topic만 지문에 반영해서 새로 호출하게 한다 (2026-08-31).
            fingerprint_extra["topic"] = search_kwargs.get("topic")
        elif lane.provider == "serpapi":
            if lane.current_domains is None:
                bundle_key = f"{lane.lv2_id}::{lane.type_name}"
                # 순수 LRU 대신 도메인 점수(추출 성공 이력) 우선, 동점이면 LRU로 고른다.
                bundle = scoring.pick_scored_bundle(conn, bundles_repo, bundle_key, epsilon=exploration_epsilon)
                if bundle is None:
                    result.warnings.append(
                        f"{lane.lv2_id}::{lane.type_name} (serpapi): 사용 가능한 도메인 번들이 없어 중단합니다."
                    )
                    continue
                lane.current_domains = bundle.domains
                bundles_repo.mark_used(conn, bundle_key, bundle.bundle_index)
            search_kwargs.update(allowed_domains=lane.current_domains, start=lane.page_start)
            fingerprint_extra.update(domains=lane.current_domains, start=lane.page_start)

        fingerprint = build_fingerprint(
            provider=lane.provider, query_text=query_row["query_text"],
            date_from=lane.date_from, date_to=lane.date_to, extra=fingerprint_extra,
        )

        cached = exec_repo.get_by_fingerprint(conn, fingerprint)
        if cached is not None:
            # 이미 같은 조건(같은 도메인 번들 포함)으로 검색해봤다 — 다시 부르지 않는다.
            page_result_count = cached["result_count"] or 0
            returned_count = page_result_count
            lane.collected += page_result_count
        else:
            provider_rate = configs.get("retry_policy", {}).get("rate_limit", {}).get(lane.provider, {})
            throttle(lane.provider, provider_rate.get("min_interval_seconds", 0))
            try:
                response = providers[lane.provider].search(
                    query_row["query_text"], date_from=lane.date_from, date_to=lane.date_to,
                    **search_kwargs,
                )
            except Exception as exc:
                quota_error = classify_quota_error(lane.provider, exc)
                capped_providers.add(lane.provider)
                if quota_error is not None:
                    result.warnings.append(
                        f"{lane.provider}: API 사용량 한도를 초과해 더 이상 호출할 수 없습니다 — "
                        "이 provider는 건너뛰고 지금까지 모은 결과로 계속 진행합니다."
                    )
                else:
                    # 사용량 문제가 아닌 예상 못 한 오류(라이브러리 버그·응답 형식 변경 등)는 같은
                    # 원인으로 계속 실패할 가능성이 높아, 여기서 raise해서 전체를 죽이는 대신 이
                    # provider만 멈추고 지금까지 모은 result.candidates는 그대로 반환한다 — 이미
                    # 성공한 검색은 fingerprint가 기록돼 재실행 시 API를 다시 안 부르고, 지금 실패한
                    # 검색어는 fingerprint가 안 남아 status=generated 그대로라 다음 실행에서 정상
                    # 재시도된다.
                    result.warnings.append(
                        f"{lane.provider}: 검색 중 예상하지 못한 오류가 발생해 이 provider 검색을 "
                        f"중단합니다 ({type(exc).__name__}: {exc}). 지금까지 찾은 "
                        f"{len(result.candidates)}건으로 계속 진행합니다."
                    )
                continue
            exec_id, _ = exec_repo.start_execution(
                conn, run_id=run_id, query_id=query_row["id"],
                request_params=response.request_params, request_fingerprint=fingerprint,
            )
            exec_repo.finish_execution(
                conn, exec_id, status="success",
                result_count=len(response.results), credit_usage=response.usage,
            )
            calls_used[lane.provider] += 1
            group_calls_used[(lane.lv2_id, lane.provider)] += 1

            result.provider_usage.setdefault(lane.provider, []).append(response.usage)
            result.candidates.extend(
                ScheduledCandidate(
                    lv2_id=lane.lv2_id, type_name=lane.type_name, provider=lane.provider,
                    query_id=query_row["id"], url=item.url, rank=item.rank,
                    relevance_score=item.relevance_score,
                )
                for item in response.results
            )
            returned_count = len(response.results)
            page_result_count = 0
            for item in response.results:
                normalized_url = normalize_url(item.url)
                if normalized_url in lane.seen_urls:
                    continue
                lane.seen_urls.add(normalized_url)
                exists = conn.execute(
                    "SELECT 1 FROM contents WHERE canonical_url = ? LIMIT 1", (normalized_url,)
                ).fetchone()
                if exists is None:
                    page_result_count += 1
            lane.collected += page_result_count

        provider_cfg = configs.get("providers", {}).get("serpapi", {})
        # 기본값 10은 serpapi_provider.py가 실제로 요청에 쓰는 기본값과 반드시 같아야 한다.
        page_size = provider_cfg.get("max_results_per_request", 10)
        max_pages = provider_cfg.get("max_pages_per_query", 1)
        continue_query = (
            lane.provider == "serpapi"
            and returned_count >= page_size
            and lane.collected < lane.target
            and lane.page_start // page_size + 1 < max_pages
        )
        if continue_query:
            lane.page_start += page_size
        else:
            queries_repo.update_status(conn, query_row["id"], "used")
            lane.current_query = None
            lane.page_start = 0
            lane.current_domains = None

        if not lane.done:
            queue.append(lane)

    return result
