"""실행 전 점검: 실제 API를 부르지 않고 이번 실행에서 무슨 일이 일어날지 요약한다."""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from src.config.validator import check_provider_api_keys
from src.discovery.allocator import has_serpapi_domains
from src.query.repository import list_active_queries


@dataclass
class TypePreflight:
    lv2_id: str
    type_name: str
    candidate_target: int
    tavily_query_count: int
    serpapi_query_count: int
    has_serpapi_domain: bool


@dataclass
class PreflightReport:
    missing_api_keys: list[str] = field(default_factory=list)
    type_reports: list[TypePreflight] = field(default_factory=list)
    domain_missing_warnings: list[str] = field(default_factory=list)
    no_query_warnings: list[str] = field(default_factory=list)
    max_requests_estimate: int = 0   # 후보를 다 못 채워 모든 검색어를 다 쓰는 최악의 경우
    max_requests_by_provider: dict[str, int] = field(default_factory=dict)  # provider별 내역

    @property
    def can_run(self) -> bool:
        return not self.missing_api_keys and any(
            t.tavily_query_count or t.serpapi_query_count for t in self.type_reports
        )


def run_preflight(
    conn: sqlite3.Connection, configs: dict, targets: list[tuple[str, str]],
    *, target_count: int, candidate_multiplier: float,
) -> PreflightReport:
    report = PreflightReport(missing_api_keys=check_provider_api_keys(configs["providers"]))
    type_domains_cfg = configs["type_domains"]["types"]
    blacklist_domains = configs["blacklist"]["domains"]
    # serpapi는 검색어 하나가 목표 미달 시 이 값만큼 페이지를 넘겨가며 재호출될 수 있다
    # (scheduler.py의 continue_query) — worst-case 추정에도 반영해야 한다.
    serpapi_max_pages = configs["providers"]["serpapi"].get("max_pages_per_query", 1)
    tavily_page_size = configs["providers"].get("tavily", {}).get("max_results_per_request", 20)
    serpapi_page_size = configs["providers"].get("serpapi", {}).get("max_results_per_request", 10)
    # scheduler.py의 lane_call_budget_multiplier와 같은 안전장치 — target을 못 채워도 검색어를
    # 무한정 다 쓰지 않고 이 배수에서 포기하므로, worst-case 추정도 이 상한을 넘지 않는다.
    budget_multiplier = configs.get("collection", {}).get("scheduling", {}).get("lane_call_budget_multiplier", 2.0)
    # target_count는 LV2 기준 목표다 — 같은 LV2에 type이 여럿이면 나눠 갖는다 (나머지는 올림).
    types_per_lv2 = Counter(lv2_id for lv2_id, _ in targets)

    # (lv2, provider) 하나가 콜 예산을 공유한다(scheduler.py와 동일 — type마다 따로 곱하면 type
    # 수만큼 배수가 커진다). 그래서 후보 target/검색어 가용량도 (lv2, provider) 단위로 먼저 모은다.
    group_candidate_target: dict[tuple[str, str], int] = {}
    group_query_worst_case: dict[tuple[str, str], int] = {}

    for lv2_id, type_name in targets:
        tavily_queries = list_active_queries(conn, taxonomy_lv2=lv2_id, type_name=type_name, provider="tavily")
        serpapi_queries = list_active_queries(conn, taxonomy_lv2=lv2_id, type_name=type_name, provider="serpapi")
        has_domain = has_serpapi_domains(type_domains_cfg, type_name, blacklist_domains, lv2_id)
        per_type_target_count = math.ceil(target_count / types_per_lv2[lv2_id])
        candidate_target = math.ceil(per_type_target_count * candidate_multiplier)

        report.type_reports.append(TypePreflight(
            lv2_id=lv2_id, type_name=type_name, candidate_target=candidate_target,
            tavily_query_count=len(tavily_queries), serpapi_query_count=len(serpapi_queries),
            has_serpapi_domain=has_domain,
        ))
        if not has_domain:
            report.domain_missing_warnings.append(
                f"{lv2_id}::{type_name}: SerpAPI 허용 도메인이 없어 전량 Tavily로 진행됩니다."
            )
        if not tavily_queries and not serpapi_queries:
            report.no_query_warnings.append(f"{lv2_id}::{type_name}: 사용할 검색어가 없어 건너뜁니다.")

        tavily_group, serpapi_group = (lv2_id, "tavily"), (lv2_id, "serpapi")
        group_candidate_target[tavily_group] = group_candidate_target.get(tavily_group, 0) + candidate_target
        group_candidate_target[serpapi_group] = group_candidate_target.get(serpapi_group, 0) + candidate_target
        group_query_worst_case[tavily_group] = group_query_worst_case.get(tavily_group, 0) + len(tavily_queries)
        group_query_worst_case[serpapi_group] = (
            group_query_worst_case.get(serpapi_group, 0) + len(serpapi_queries) * serpapi_max_pages
        )

    # 콜당 최대치를 다 채운다는 이상적인 가정으로 (lv2, provider) 전체가 필요한 콜 수 x
    # budget_multiplier — lane이 실제로 포기하는 상한(scheduler._build_lanes와 동일한 공식)과
    # 검색어 소진 상한 중 작은 쪽.
    for (lv2_id, provider), query_worst_case in group_query_worst_case.items():
        page_size = tavily_page_size if provider == "tavily" else serpapi_page_size
        candidate_target = group_candidate_target[(lv2_id, provider)]
        budget_cap = max(1, math.ceil(candidate_target / page_size)) * budget_multiplier
        estimate = min(query_worst_case, budget_cap)
        report.max_requests_estimate += estimate
        report.max_requests_by_provider[provider] = report.max_requests_by_provider.get(provider, 0) + estimate

    return report
