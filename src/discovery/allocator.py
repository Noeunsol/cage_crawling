"""provider별 도메인 자동 분리 (6.1~6.3절).

Tavily 최종 제외 도메인 = 이 type의 SerpAPI 허용 도메인 ∪ 공통 블랙리스트.
이렇게 해서 두 provider가 같은 도메인을 중복 검색하지 않는다.
"""

from __future__ import annotations


def serpapi_allowed_domains(
    type_domains_cfg: dict, type_name: str, blacklist_domains: list[str] | None = None,
) -> list[str]:
    """블랙리스트 도메인은 여기서 걸러진다 — type_domains.yaml에 실수로 남아 있어도 실제 검색엔 안 쓰인다."""
    domains = type_domains_cfg.get(type_name, {}).get("serpapi_allowed_domains", [])
    if blacklist_domains:
        domains = [d for d in domains if d not in blacklist_domains]
    return domains


def has_serpapi_domains(
    type_domains_cfg: dict, type_name: str, blacklist_domains: list[str] | None = None,
) -> bool:
    return bool(serpapi_allowed_domains(type_domains_cfg, type_name, blacklist_domains))


def tavily_exclude_domains(
    type_domains_cfg: dict, blacklist_domains: list[str], type_name: str
) -> list[str]:
    allowed = serpapi_allowed_domains(type_domains_cfg, type_name)
    return sorted(set(allowed) | set(blacklist_domains))
