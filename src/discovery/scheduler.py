"""검색 요청을 언제·얼마나 보낼지 결정하는 스케줄러 (7절).

이 모듈은 discovery(검색 API 호출)까지만 담당한다. 찾은 URL을 실제로 가져와 저장하는 일은
fetcher/filter 단계(Phase 7~9)의 몫이라, 여기서는 후보 URL을 메모리 상에서 모아 돌려준다.
UI(session_state)에는 의존하지 않는다 — 날짜/비율은 이미 계산된 값으로 받는다.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import date

from src.discovery.allocator import has_serpapi_domains, serpapi_allowed_domains, tavily_exclude_domains
from src.discovery.base import build_fingerprint
from src.query.repository import list_active_queries
from src.storage.repositories import domain_bundles as bundles_repo
from src.storage.repositories import queries as queries_repo
from src.storage.repositories import query_executions as exec_repo
from src.utils.rate_limit import throttle


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

    @property
    def done(self) -> bool:
        return self.collected >= self.target or not self.queries


def _build_lanes(
    conn, configs, targets, *, target_count, candidate_multiplier,
    date_range_by_lv2, provider_ratio_by_lv2,
) -> tuple[list[_Lane], list[str]]:
    type_domains_cfg = configs["type_domains"]["types"]
    alias_groups = configs["domain_aliases"]["groups"]
    blacklist_domains = configs["blacklist"]["domains"]
    default_ratio = configs["collection"]["provider_ratio"]["default"]
    # target_count는 LV2 기준 목표다 — 같은 LV2에 type이 여럿이면 나눠 갖는다 (나머지는 올림).
    types_per_lv2 = Counter(lv2_id for lv2_id, _ in targets)

    lanes: list[_Lane] = []
    warnings: list[str] = []

    for lv2_id, type_name in targets:
        date_from, date_to = date_range_by_lv2[lv2_id]
        ratio = provider_ratio_by_lv2.get(lv2_id, default_ratio)
        per_type_target_count = math.ceil(target_count / types_per_lv2[lv2_id])
        total = math.ceil(per_type_target_count * candidate_multiplier)

        config_has_domains = has_serpapi_domains(type_domains_cfg, type_name, blacklist_domains)
        if config_has_domains:
            # 도메인을 최대 3개씩 번들로 묶고(alias는 항상 같은 묶음), 상태를 DB에 맞춰둔다.
            bundles_repo.sync_bundles(
                conn, type_name,
                serpapi_allowed_domains(type_domains_cfg, type_name, blacklist_domains), alias_groups,
            )
        serpapi_available = config_has_domains and bundles_repo.has_enabled_bundle(conn, type_name)

        if serpapi_available:
            tavily_target = round(total * ratio["tavily"] / 100)
            serpapi_target = total - tavily_target
        else:
            # 6.3절: 도메인이 없거나(config) 번들이 전부 비활성화된 type은 전량 Tavily로 이관한다.
            tavily_target, serpapi_target = total, 0
            reason = "SerpAPI 허용 도메인이 없어" if not config_has_domains else "모든 도메인 번들이 비활성화돼 있어"
            warnings.append(f"{lv2_id}::{type_name}: {reason} 목표 {total}건 전량을 Tavily로 진행합니다.")

        provider_targets = [
            ("tavily", tavily_target, {"exclude_domains": tavily_exclude_domains(
                type_domains_cfg, blacklist_domains, type_name)}),
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
                queries=list(active_queries),
            ))

    return lanes, warnings


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
) -> SchedulerResult:
    """활성 type이 고르게 검색되도록 round-robin으로 provider를 호출한다 (7.4, 7.5절).

    - 새 요청을 시작하기 직전에만 목표 달성 여부를 확인한다. 이미 시작한 요청은 끝까지 처리한다.
    - 동일 조건(같은 fingerprint)으로 이미 검색한 적이 있으면 API를 다시 부르지 않는다 (10.1절).
    - max_calls_by_provider가 있으면 provider별 실제 API 호출 수(캐시 적중 제외)에 상한을 건다
      (실험용 — target_count 계산과 무관하게 이번 실행만 강제로 줄인다).
    """
    lanes, warnings = _build_lanes(
        conn, configs, targets, target_count=target_count, candidate_multiplier=candidate_multiplier,
        date_range_by_lv2=date_range_by_lv2, provider_ratio_by_lv2=provider_ratio_by_lv2,
    )
    result = SchedulerResult(warnings=warnings)
    queue = deque(lane for lane in lanes if not lane.done)
    calls_used = Counter()
    capped_providers: set[str] = set()

    def _is_capped(provider: str) -> bool:
        if not max_calls_by_provider or provider not in max_calls_by_provider:
            return False
        return calls_used[provider] >= max_calls_by_provider[provider]

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
        query_row = lane.queries.pop(0)

        search_kwargs = dict(lane.search_kwargs)
        fingerprint_extra = None
        if lane.provider == "serpapi":
            # 이미 계산된 예산(이 while 루프) 안에서만 번들을 고른다 — 로테이션 전용 추가 요청은 없다.
            bundle = bundles_repo.pick_bundle(conn, lane.type_name)
            if bundle is None:
                result.warnings.append(
                    f"{lane.lv2_id}::{lane.type_name} (serpapi): 사용 가능한 도메인 번들이 없어 중단합니다."
                )
                continue  # 이 lane은 더 진행할 수 없다 — 큐에 다시 넣지 않는다.
            search_kwargs["allowed_domains"] = bundle.domains
            fingerprint_extra = {"domains": bundle.domains}
            # 실제 호출 여부와 무관하게 이번 라운드에 이 번들을 배정했다는 사실 자체가 순환을 진행시킨다.
            bundles_repo.mark_used(conn, lane.type_name, bundle.bundle_index)

        fingerprint = build_fingerprint(
            provider=lane.provider, query_text=query_row["query_text"],
            date_from=lane.date_from, date_to=lane.date_to, extra=fingerprint_extra,
        )

        cached = exec_repo.get_by_fingerprint(conn, fingerprint)
        if cached is not None:
            # 이미 같은 조건(같은 도메인 번들 포함)으로 검색해봤다 — 다시 부르지 않는다.
            lane.collected += cached["result_count"] or 0
        else:
            provider_rate = configs.get("retry_policy", {}).get("rate_limit", {}).get(lane.provider, {})
            throttle(lane.provider, provider_rate.get("min_interval_seconds", 0))
            response = providers[lane.provider].search(
                query_row["query_text"], date_from=lane.date_from, date_to=lane.date_to,
                **search_kwargs,
            )
            exec_id, _ = exec_repo.start_execution(
                conn, run_id=run_id, query_id=query_row["id"],
                request_params=response.request_params, request_fingerprint=fingerprint,
            )
            exec_repo.finish_execution(
                conn, exec_id, status="success",
                result_count=len(response.results), credit_usage=response.usage,
            )
            queries_repo.update_status(conn, query_row["id"], "used")
            calls_used[lane.provider] += 1

            result.provider_usage.setdefault(lane.provider, []).append(response.usage)
            result.candidates.extend(
                ScheduledCandidate(
                    lv2_id=lane.lv2_id, type_name=lane.type_name, provider=lane.provider,
                    query_id=query_row["id"], url=item.url, rank=item.rank,
                    relevance_score=item.relevance_score,
                )
                for item in response.results
            )
            lane.collected += len(response.results)

        if not lane.done:
            queue.append(lane)

    return result
