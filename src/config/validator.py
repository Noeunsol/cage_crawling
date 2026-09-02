"""configs/*.yaml 내용을 검증해서, 잘못되거나 빠진 설정을 사람이 읽을 수 있는 에러로 알려준다."""

from __future__ import annotations

import os


class ConfigError(Exception):
    """설정 검증 실패. args[0]에 문제 목록(list[str])을 담는다."""

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("설정 오류:\n" + "\n".join(f"- {i}" for i in issues))


def _require(cond: bool, issues: list[str], message: str) -> None:
    if not cond:
        issues.append(message)


def validate_configs(configs: dict[str, dict]) -> None:
    """configs 딕셔너리를 검증한다. 문제가 있으면 ConfigError를 던진다."""
    issues: list[str] = []

    _validate_app(configs.get("app", {}), issues)
    _validate_collection(configs.get("collection", {}), issues)
    _validate_providers(configs.get("providers", {}), issues)
    _validate_taxonomy(configs.get("taxonomy", {}), issues)
    _validate_type_domains(configs.get("type_domains", {}), issues)
    _validate_domain_aliases(configs.get("domain_aliases", {}), issues)
    _validate_blacklist(configs.get("blacklist", {}), issues)
    _validate_domain_overlap(
        configs.get("type_domains", {}), configs.get("blacklist", {}), issues,
    )
    _validate_extraction(configs.get("extraction", {}), issues)
    _validate_retry_policy(configs.get("retry_policy", {}), issues)

    if issues:
        raise ConfigError(issues)


def _validate_app(cfg: dict, issues: list[str]) -> None:
    _require("path" in cfg.get("database", {}), issues, "app.yaml: database.path가 없습니다.")
    _require("final_dir" in cfg.get("export", {}), issues, "app.yaml: export.final_dir가 없습니다.")


def _validate_collection(cfg: dict, issues: list[str]) -> None:
    defaults = cfg.get("defaults", {})
    _require(defaults.get("target_count", 0) > 0, issues,
             "collection.yaml: defaults.target_count는 1 이상이어야 합니다.")
    _require(defaults.get("candidate_multiplier", 0) > 0, issues,
             "collection.yaml: defaults.candidate_multiplier는 0보다 커야 합니다.")

    options = cfg.get("candidate_multiplier_options", [])
    _require(defaults.get("candidate_multiplier") in options, issues,
              "collection.yaml: defaults.candidate_multiplier가 candidate_multiplier_options 안에 없습니다.")

    qgen = cfg.get("query_generation", {})
    _require(qgen.get("tavily_count_per_type", 0) > 0, issues,
             "collection.yaml: query_generation.tavily_count_per_type은 1 이상이어야 합니다.")
    _require(qgen.get("serpapi_count_per_type", 0) > 0, issues,
             "collection.yaml: query_generation.serpapi_count_per_type은 1 이상이어야 합니다.")

    ratio = cfg.get("provider_ratio", {}).get("default", {})
    total = ratio.get("tavily", 0) + ratio.get("serpapi", 0)
    _require(total == 100, issues,
             f"collection.yaml: provider_ratio.default 합은 100이어야 합니다 (현재 {total}).")

    for lv2_id, lv2_ratio in cfg.get("provider_ratio", {}).get("by_lv2", {}).items():
        lv2_total = lv2_ratio.get("tavily", 0) + lv2_ratio.get("serpapi", 0)
        _require(lv2_total == 100, issues,
                 f"collection.yaml: provider_ratio.by_lv2.{lv2_id} 합은 100이어야 합니다 (현재 {lv2_total}).")

    for lv2_id, months in cfg.get("dates", {}).get("date_range_months_by_lv2", {}).items():
        _require(isinstance(months, int) and months > 0, issues,
                 f"collection.yaml: dates.date_range_months_by_lv2.{lv2_id}는 1 이상의 정수여야 합니다.")


def _validate_providers(cfg: dict, issues: list[str]) -> None:
    _require("model" in cfg.get("openai", {}), issues, "providers.yaml: openai.model이 없습니다.")
    _require("query_generation_model" in cfg.get("openai", {}), issues,
             "providers.yaml: openai.query_generation_model이 없습니다.")
    for key in (
        "query_generation_input_price_per_1m_usd", "query_generation_output_price_per_1m_usd",
        "web_search_price_per_1k_calls_usd",
    ):
        _require(cfg.get("openai", {}).get(key, -1) >= 0, issues, f"providers.yaml: openai.{key}가 없거나 잘못됐습니다.")
    for provider in ("openai", "tavily", "serpapi"):
        _require("api_key_env" in cfg.get(provider, {}), issues,
                 f"providers.yaml: {provider}.api_key_env가 없습니다.")
    serpapi = cfg.get("serpapi", {})
    _require(serpapi.get("max_pages_per_query", 0) > 0, issues,
             "providers.yaml: serpapi.max_pages_per_query는 1 이상이어야 합니다.")


# 있으면 필수 필드는 아니지만(1_C_Self_Harm 등 일부 type에만 있음), 있을 땐 문자열 리스트여야 한다.
# 오타로 엉뚱한 키가 생겨도 .get() 기본값으로 조용히 무시되지 않도록 타입만 확인한다.
_OPTIONAL_TYPE_LIST_FIELDS = ("search_vocabulary", "collection_exclude_criteria",
                              "harm_positive_exclude_criteria", "query_axes")
_OPTIONAL_LV2_LIST_FIELDS = ("lv2_fallback_vocabulary",)


def _require_str_list(value, issues: list[str], where: str) -> None:
    _require(
        isinstance(value, list) and all(isinstance(v, str) for v in value),
        issues, f"{where}는 문자열 리스트여야 합니다.",
    )


def _validate_taxonomy(cfg: dict, issues: list[str]) -> None:
    groups = cfg.get("taxonomy", [])
    _require(len(groups) > 0, issues, "taxonomy.yaml: taxonomy 목록이 비어 있습니다.")

    for group in groups:
        lv2_id = group.get("lv2_id", "<unknown>")
        types = group.get("types", [])
        _require(len(types) > 0, issues, f"taxonomy.yaml: {lv2_id}에 type이 하나도 없습니다.")
        for field in _OPTIONAL_LV2_LIST_FIELDS:
            if field in group:
                _require_str_list(group[field], issues, f"taxonomy.yaml: {lv2_id}.{field}")
        for t in types:
            for field in ("name", "definition", "description", "include_criteria",
                          "exclude_criteria", "enabled"):
                _require(field in t, issues,
                         f"taxonomy.yaml: {lv2_id}.{t.get('name', '<unknown>')}에 '{field}' 필드가 없습니다.")
            for field in _OPTIONAL_TYPE_LIST_FIELDS:
                if field in t:
                    _require_str_list(t[field], issues, f"taxonomy.yaml: {lv2_id}.{t.get('name')}.{field}")


def _validate_type_domains(cfg: dict, issues: list[str]) -> None:
    for key, info in cfg.get("types", {}).items():
        if not isinstance(info, dict):
            _require(False, issues, f"type_domains.yaml: {key}는 매핑(값이 있는 항목)이어야 합니다.")
            continue
        entries = info.items() if "serpapi_allowed_domains" not in info else [(key, info)]
        for type_name, type_info in entries:
            _require(isinstance(type_info.get("serpapi_allowed_domains"), list), issues,
                     f"type_domains.yaml: {key}.{type_name}.serpapi_allowed_domains는 리스트여야 합니다.")


def _validate_blacklist(cfg: dict, issues: list[str]) -> None:
    _require(isinstance(cfg.get("domains"), list), issues, "blacklist.yaml: domains는 리스트여야 합니다.")


def _validate_domain_overlap(type_domains_cfg: dict, blacklist_cfg: dict, issues: list[str]) -> None:
    allowed: set[str] = set()
    for info in type_domains_cfg.get("types", {}).values():
        entries = info.values() if isinstance(info, dict) and "serpapi_allowed_domains" not in info else [info]
        for type_info in entries:
            if isinstance(type_info, dict):
                allowed.update(type_info.get("serpapi_allowed_domains", []))

    overlap = allowed & set(blacklist_cfg.get("domains", []))
    _require(
        not overlap, issues,
        f"type_domains.yaml 허용 도메인이 blacklist.yaml에도 있습니다: {', '.join(sorted(overlap))}",
    )


def _validate_extraction(cfg: dict, issues: list[str]) -> None:
    cleaning = cfg.get("cleaning", {})
    _require(isinstance(cleaning.get("trailing_section_markers"), list), issues,
             "extraction.yaml: cleaning.trailing_section_markers는 리스트여야 합니다.")
    _require(isinstance(cleaning.get("remove_duplicate_paragraphs"), bool), issues,
             "extraction.yaml: cleaning.remove_duplicate_paragraphs는 true/false여야 합니다.")
    minimum = cleaning.get("duplicate_paragraph_min_length")
    _require(isinstance(minimum, int) and minimum > 0, issues,
             "extraction.yaml: cleaning.duplicate_paragraph_min_length는 1 이상의 정수여야 합니다.")


def _validate_domain_aliases(cfg: dict, issues: list[str]) -> None:
    for canonical, aliases in cfg.get("groups", {}).items():
        _require(isinstance(aliases, list) and len(aliases) > 0, issues,
                 f"domain_aliases.yaml: groups.{canonical}는 비어있지 않은 리스트여야 합니다.")


def _validate_retry_policy(cfg: dict, issues: list[str]) -> None:
    _require(len(cfg.get("reasons", {})) > 0, issues, "retry_policy.yaml: reasons가 비어 있습니다.")
    _require(len(cfg.get("rate_limit", {})) > 0, issues, "retry_policy.yaml: rate_limit이 비어 있습니다.")


def check_provider_api_keys(providers_cfg: dict) -> list[str]:
    """provider별 API key 환경변수가 실제로 설정돼 있는지 확인한다.

    반환값은 "키가 없는 provider" 설명 목록. Streamlit preflight 화면에서 그대로 보여주면 된다.
    """
    missing = []
    for provider in ("openai", "tavily", "serpapi"):
        env_name = providers_cfg.get(provider, {}).get("api_key_env")
        if not env_name or not os.environ.get(env_name):
            missing.append(f"{provider} (환경변수 {env_name} 미설정)")
    return missing
