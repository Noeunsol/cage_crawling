"""Phase 1 완료 조건 검증: 설정이 전부 로드되고, 문제가 있으면 이해 가능한 에러가 난다."""

import pytest

from src.config.loader import load_all_configs
from src.config.validator import ConfigError, check_provider_api_keys, validate_configs


def test_load_all_configs_succeeds():
    configs = load_all_configs()
    assert set(configs) == {
        "app", "collection", "providers", "extraction", "retry_policy",
        "taxonomy", "type_domains", "domain_aliases", "blacklist", "logging",
    }


def test_real_configs_pass_validation():
    configs = load_all_configs()
    validate_configs(configs)  # 예외가 나지 않아야 정상


def test_allowed_domain_cannot_also_be_blacklisted():
    configs = load_all_configs()
    configs["blacklist"]["domains"].append("velog.io")

    with pytest.raises(ConfigError) as exc_info:
        validate_configs(configs)

    assert any("velog.io" in issue for issue in exc_info.value.issues)


def test_broken_type_domains_entry_reports_readable_error_instead_of_crashing():
    configs = load_all_configs()
    configs["type_domains"]["types"]["broken_type"] = None  # `broken_type:` 처럼 값 없는 YAML 키

    with pytest.raises(ConfigError) as exc_info:
        validate_configs(configs)

    assert any("broken_type" in issue for issue in exc_info.value.issues)


def test_taxonomy_has_75_types_in_19_groups():
    configs = load_all_configs()
    groups = configs["taxonomy"]["taxonomy"]
    assert len(groups) == 19
    assert sum(len(g["types"]) for g in groups) == 75


def test_missing_required_field_raises_readable_error():
    configs = load_all_configs()
    del configs["providers"]["openai"]["model"]

    with pytest.raises(ConfigError) as exc_info:
        validate_configs(configs)

    assert any("openai.model" in issue for issue in exc_info.value.issues)


def test_bad_provider_ratio_is_caught():
    configs = load_all_configs()
    configs["collection"]["provider_ratio"]["default"] = {"tavily": 60, "serpapi": 60}

    with pytest.raises(ConfigError) as exc_info:
        validate_configs(configs)

    assert any("provider_ratio" in issue for issue in exc_info.value.issues)


def test_bad_provider_ratio_by_lv2_is_caught():
    configs = load_all_configs()
    configs["collection"]["provider_ratio"]["by_lv2"]["1_A_Toxic_Language"] = {"tavily": 60, "serpapi": 60}

    with pytest.raises(ConfigError) as exc_info:
        validate_configs(configs)

    assert any("by_lv2.1_A_Toxic_Language" in issue for issue in exc_info.value.issues)


def test_provider_ratio_by_lv2_covers_all_19_lv2_groups():
    configs = load_all_configs()
    by_lv2 = configs["collection"]["provider_ratio"]["by_lv2"]
    lv2_ids = {g["lv2_id"] for g in configs["taxonomy"]["taxonomy"]}
    assert set(by_lv2) == lv2_ids


def test_check_provider_api_keys_reports_missing(monkeypatch):
    configs = load_all_configs()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_KEY", raising=False)

    missing = check_provider_api_keys(configs["providers"])

    assert len(missing) == 3
