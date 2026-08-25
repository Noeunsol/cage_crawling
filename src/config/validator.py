"""configs/*.yaml 내용을 검증해서, 잘못되거나 빠진 설정을 사람이 읽을 수 있는 에러로 알려준다.

Phase 1 완료 조건(문서 18절 Phase 1):
  "모든 기본값이 YAML에서 로드됨" / "누락·잘못된 설정을 이해 가능한 오류로 제공"
"""

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
    for provider in ("openai", "tavily", "serpapi"):
        _require("api_key_env" in cfg.get(provider, {}), issues,
                 f"providers.yaml: {provider}.api_key_env가 없습니다.")


def _validate_taxonomy(cfg: dict, issues: list[str]) -> None:
    groups = cfg.get("taxonomy", [])
    _require(len(groups) > 0, issues, "taxonomy.yaml: taxonomy 목록이 비어 있습니다.")

    for group in groups:
        lv2_id = group.get("lv2_id", "<unknown>")
        types = group.get("types", [])
        _require(len(types) > 0, issues, f"taxonomy.yaml: {lv2_id}에 type이 하나도 없습니다.")
        for t in types:
            for field in ("name", "definition", "description", "include_criteria",
                          "exclude_criteria", "enabled"):
                _require(field in t, issues,
                         f"taxonomy.yaml: {lv2_id}.{t.get('name', '<unknown>')}에 '{field}' 필드가 없습니다.")


def _validate_type_domains(cfg: dict, issues: list[str]) -> None:
    for type_name, info in cfg.get("types", {}).items():
        _require(isinstance(info.get("serpapi_allowed_domains"), list), issues,
                 f"type_domains.yaml: {type_name}.serpapi_allowed_domains는 리스트여야 합니다.")


def _validate_blacklist(cfg: dict, issues: list[str]) -> None:
    _require(isinstance(cfg.get("domains"), list), issues, "blacklist.yaml: domains는 리스트여야 합니다.")


def _validate_domain_aliases(cfg: dict, issues: list[str]) -> None:
    for canonical, aliases in cfg.get("groups", {}).items():
        _require(isinstance(aliases, list) and len(aliases) > 0, issues,
                 f"domain_aliases.yaml: groups.{canonical}는 비어있지 않은 리스트여야 합니다.")


def _validate_retry_policy(cfg: dict, issues: list[str]) -> None:
    _require(len(cfg.get("reasons", {})) > 0, issues, "retry_policy.yaml: reasons가 비어 있습니다.")
    _require(len(cfg.get("rate_limit", {})) > 0, issues, "retry_policy.yaml: rate_limit이 비어 있습니다.")


def check_provider_api_keys(providers_cfg: dict) -> list[str]:
    """provider별 API key 환경변수가 실제로 설정돼 있는지 확인한다.

    반환값은 "키가 없는 provider" 설명 목록 (16.1절: "key 누락은 실행 전 검사에서
    provider별로 명확히 표시"). Streamlit preflight 화면에서 그대로 보여주면 된다.
    """
    missing = []
    for provider in ("openai", "tavily", "serpapi"):
        env_name = providers_cfg.get(provider, {}).get("api_key_env")
        if not env_name or not os.environ.get(env_name):
            missing.append(f"{provider} (환경변수 {env_name} 미설정)")
    return missing
