"""ui/common.py의 순수 로직(전역/LV2별 기본값 우선순위 계산)을 검증한다."""

from datetime import date

from ui.common import (
    default_date_range_for_lv2, effective_date_range, effective_provider_ratio, exclusion_reason_label,
    suggested_query_counts,
)


def _configs(date_range_months_by_lv2=None, provider_ratio_by_lv2=None):
    return {
        "collection": {
            "dates": {"date_range_months_by_lv2": date_range_months_by_lv2 or {}},
            "provider_ratio": {
                "default": {"tavily": 50, "serpapi": 50},
                "by_lv2": provider_ratio_by_lv2 or {},
            },
        },
    }


def test_default_date_range_for_lv2_subtracts_months_correctly():
    configs = _configs(date_range_months_by_lv2={"3_G_Misinformation_and_Disinformation": 6})
    d_from, d_to = default_date_range_for_lv2(configs, "3_G_Misinformation_and_Disinformation")

    assert d_to == date.today()
    months_diff = (d_to.year - d_from.year) * 12 + (d_to.month - d_from.month)
    assert months_diff == 6


def test_default_date_range_for_lv2_returns_none_when_not_configured():
    configs = _configs()
    assert default_date_range_for_lv2(configs, "1_A_Toxic_Language") is None


def test_effective_date_range_priority_manual_over_config_over_global():
    configs = _configs(date_range_months_by_lv2={"3_G_Misinformation_and_Disinformation": 6})
    global_range = (date(2020, 1, 1), date(2021, 1, 1))

    # 1) 아무 override도 없으면 전역값
    setup = {"date_from": global_range[0], "date_to": global_range[1], "date_overrides": {}}
    assert effective_date_range(setup, configs, "1_A_Toxic_Language") == global_range

    # 2) config에 LV2별 기본값이 있으면 그게 자동 적용됨 (전역보다 우선)
    lv2_result = effective_date_range(setup, configs, "3_G_Misinformation_and_Disinformation")
    assert lv2_result != global_range
    assert lv2_result[1] == date.today()

    # 3) 사용자가 화면에서 수동 override하면 그게 최우선
    setup["date_overrides"]["3_G_Misinformation_and_Disinformation"] = {
        "date_from": date(2025, 1, 1), "date_to": date(2025, 6, 1),
    }
    assert effective_date_range(setup, configs, "3_G_Misinformation_and_Disinformation") == (
        date(2025, 1, 1), date(2025, 6, 1),
    )


def test_effective_provider_ratio_priority_manual_over_config_over_global():
    configs = _configs(provider_ratio_by_lv2={"1_C_Self_Harm": {"tavily": 30, "serpapi": 70}})
    setup = {"provider_ratio": {"tavily": 50, "serpapi": 50}, "provider_ratio_overrides": {}}

    assert effective_provider_ratio(setup, configs, "1_A_Toxic_Language") == {"tavily": 50, "serpapi": 50}
    assert effective_provider_ratio(setup, configs, "1_C_Self_Harm") == {"tavily": 30, "serpapi": 70}

    setup["provider_ratio_overrides"]["1_C_Self_Harm"] = {"tavily": 10, "serpapi": 90}
    assert effective_provider_ratio(setup, configs, "1_C_Self_Harm") == {"tavily": 10, "serpapi": 90}


def test_exclusion_reason_label_extracts_known_and_unknown_codes():
    assert exclusion_reason_label("excluded", "blacklisted_domain: 'x.com'는 블랙리스트 도메인입니다.") == "블랙리스트 도메인"
    assert exclusion_reason_label(
        "excluded", "taxonomy_mismatch: 구체적 사례 없음 | 한국 관련성: 한국 커뮤니티 글"
    ) == "taxonomy 부적합"
    assert exclusion_reason_label("excluded", "some_new_reason: 상세") == "some_new_reason"  # 모르는 코드는 원문 그대로


def test_exclusion_reason_label_blank_when_not_excluded():
    assert exclusion_reason_label("accepted", "accepted | 한국 관련성: ok") == ""
    assert exclusion_reason_label(None, None) == ""
    assert exclusion_reason_label("excluded", None) == ""


def test_suggested_query_counts_uses_accepted_conversion_and_provider_split(monkeypatch):
    configs = {
        "collection": {
            "query_generation": {"tavily_count_per_type": 1, "serpapi_count_per_type": 1, "safety_factor": 1},
            "provider_ratio": {"default": {"tavily": 60, "serpapi": 40}, "by_lv2": {}},
        },
        "providers": {
            "tavily": {"max_results_per_request": 10},
            "serpapi": {"max_results_per_request": 10},
        },
    }
    setup = {
        "target_count": 100, "candidate_multiplier": 2,
        "provider_ratio": {"tavily": 60, "serpapi": 40}, "provider_ratio_overrides": {},
    }
    multipliers = {"tavily": 2, "serpapi": 3}
    monkeypatch.setattr(
        "src.discovery.adaptive_multiplier.compute_multiplier",
        lambda _conn, _configs, _lv2, _type, provider: multipliers[provider],
    )

    assert suggested_query_counts(
        configs, setup, "LV2", conn=object(), type_name="type_a",
    ) == (12, 12)
