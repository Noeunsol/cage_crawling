"""2차 수집 설정 로더 — run.py와 review.py가 함께 쓴다."""
from __future__ import annotations

import yaml


def load_phase2_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def apply_overrides(p2: dict, overrides: dict | None) -> dict:
    """UI 슬라이더 등이 config 위에 threshold/limit을 덮어쓴다(얕은 섹션 병합)."""
    if not overrides:
        return p2
    for section in ("rerank", "acceptance", "adjudication", "limits", "target_selection",
                    "query_planner", "default_acceptance"):
        if section in overrides:
            p2.setdefault(section, {}).update(overrides[section])
    # LV2별 값(수집 기간)은 섹션이 아니라 전략 안에 있어 따로 덮는다.
    # recency_days 하나로 Tavily start_date·SerpAPI tbs·acceptance stale이 함께 움직인다.
    for lv2, values in (overrides.get("strategy_by_lv2") or {}).items():
        strategy = (p2.get("source_strategies_by_lv2") or {}).get(lv2)
        if strategy:
            strategy.update({k: v for k, v in (values or {}).items() if v})
    return p2
