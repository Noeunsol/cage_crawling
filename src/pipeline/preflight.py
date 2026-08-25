"""실행 전 점검 (14.4절): 실제 API를 부르지 않고 이번 실행에서 무슨 일이 일어날지 요약한다."""

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
    # target_count는 LV2 기준 목표다 — 같은 LV2에 type이 여럿이면 나눠 갖는다 (나머지는 올림).
    types_per_lv2 = Counter(lv2_id for lv2_id, _ in targets)

    for lv2_id, type_name in targets:
        tavily_queries = list_active_queries(conn, taxonomy_lv2=lv2_id, type_name=type_name, provider="tavily")
        serpapi_queries = list_active_queries(conn, taxonomy_lv2=lv2_id, type_name=type_name, provider="serpapi")
        has_domain = has_serpapi_domains(type_domains_cfg, type_name, blacklist_domains)
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

        report.max_requests_estimate += len(tavily_queries) + len(serpapi_queries)

    return report
