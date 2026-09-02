"""provider별 검색 도메인을 계산한다.

기본적으로 Tavily는 SerpAPI가 전담하는 도메인도 함께 제외한다 — provider를 도메인 기준으로
분리한다(2026-08-26 실측: SerpAPI 전담 도메인에서 Tavily가 찾은 accepted 콘텐츠가 0건이라,
겹쳐 검색해도 얻는 게 없고 크레딧만 낭비됨). configs/collection.yaml의
domain_partition.tavily_excludes_serpapi_domains로 끌 수 있다.
"""

from __future__ import annotations

from src.utils.urls import is_blocklisted_domain


def serpapi_allowed_domains(
    type_domains_cfg: dict, type_name: str, blacklist_domains: list[str] | None = None,
    lv2_id: str | None = None,
) -> list[str]:
    """블랙리스트 도메인은 여기서 걸러진다 — type_domains.yaml에 실수로 남아 있어도 실제 검색엔 안 쓰인다."""
    info = type_domains_cfg.get(lv2_id, {}).get(type_name, {}) if lv2_id else {}
    domains = (info or type_domains_cfg.get(type_name, {})).get("serpapi_allowed_domains", [])
    if blacklist_domains:
        domains = [d for d in domains if not is_blocklisted_domain(d, blacklist_domains)]
    return domains


def has_serpapi_domains(
    type_domains_cfg: dict, type_name: str, blacklist_domains: list[str] | None = None,
    lv2_id: str | None = None,
) -> bool:
    return bool(serpapi_allowed_domains(type_domains_cfg, type_name, blacklist_domains, lv2_id))


def tavily_exclude_domains(
    type_domains_cfg: dict,
    blacklist_domains: list[str],
    type_name: str,
    *,
    exclude_serpapi_domains: bool = True,
    lv2_id: str | None = None,
) -> list[str]:
    """Tavily 제외 도메인을 중복 없이 정렬한다.

    ``exclude_serpapi_domains``는 기본 True(provider를 도메인 기준으로 분리)다 — 모듈 docstring의
    실측 근거 참고. config 키가 실수로 빠져도 예전의 "겹쳐 검색" 동작으로 조용히 되돌아가지
    않도록, 이 함수 자체의 기본값도 실제로 채택된 쪽(True)으로 맞춰둔다.
    """
    excluded = set(blacklist_domains)
    if exclude_serpapi_domains:
        excluded.update(serpapi_allowed_domains(type_domains_cfg, type_name, lv2_id=lv2_id))
    return sorted(excluded)
