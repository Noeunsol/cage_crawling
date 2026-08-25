"""1단계: 수집 설정 (14.1절) — LV2/type 선택, LV2별 예외 설정, 목표량/배수.

기간·provider 비율은 configs/collection.yaml에 LV2별 확정값(또는 코드 기본값)이 있고,
필요한 LV2만 아래 ②카드에서 개별로 덮어쓴다. 전역으로 따로 조정하는 화면은 두지 않는다 —
어차피 카드에서 LV2별로 바꾸면 되기 때문 (사용자 결정, 2026-08-24).
"""

from __future__ import annotations

import os
from datetime import date

import streamlit as st

from ui.common import (
    accepted_counts, candidate_target, default_date_range_for_lv2, get_configs, get_db,
    get_serpapi_usage, groups_by_lv1, init_setup_state, lv1_badge, per_type_target_count,
    show_missing_api_key_banner, taxonomy_groups, type_key,
)

st.title("🎯 1. 수집 설정")
st.caption("어떤 위험 유형을, 얼마나, 언제까지 모을지 정합니다. 코드를 몰라도 이 화면만으로 실행 준비가 끝납니다.")

configs = get_configs()
conn = get_db()
show_missing_api_key_banner(configs["providers"])
init_setup_state(configs)
setup = st.session_state["setup"]
counts = accepted_counts(conn)
allow_future = configs["collection"]["dates"]["allow_future_end_date"]

# ---------------------------------------------------------------- ① LV2 선택
st.subheader("① 수집할 위험 유형 (LV2) 선택")
st.caption("큰 분류(LV1)를 펼쳐서 그 안의 세부 위험 유형(LV2)을 고르세요. 배지 색과 이모지로 대분류를 구분합니다.")

groups = taxonomy_groups(configs)
selected_set = set(setup["selected_lv2"])

for lv1_name, lv1_groups in groups_by_lv1(configs).items():
    picked_here = sum(1 for g in lv1_groups if g["lv2_id"] in selected_set)
    with st.expander(
        f"{lv1_badge(lv1_name)}  ·  {picked_here}/{len(lv1_groups)}개 선택됨",
        expanded=picked_here > 0,
    ):
        cols = st.columns(2)
        for i, g in enumerate(lv1_groups):
            with cols[i % 2]:
                checked = st.checkbox(
                    f"{g['lv2_name']}  ({len(g['types'])} type)",
                    value=g["lv2_id"] in selected_set,
                    key=f"lv2_pick_{g['lv2_id']}",
                    help=f"[{g['lv2_id']}] {g['lv2_description']}",
                )
                if checked:
                    selected_set.add(g["lv2_id"])
                else:
                    selected_set.discard(g["lv2_id"])

# taxonomy.yaml에 나온 순서를 유지하기 위해 groups를 훑으며 선택된 것만 남긴다.
setup["selected_lv2"] = [g["lv2_id"] for g in groups if g["lv2_id"] in selected_set]

if not setup["selected_lv2"]:
    st.info("위에서 LV2를 하나 이상 선택하면 나머지 설정이 나타납니다.")
    st.stop()

# ---------------------------------------------------------------- ② type별 활성화 + LV2별 예외 설정
st.subheader("② type별 활성/비활성 및 LV2별 예외 설정")
st.caption(
    "이미 충분히 모은 type은 체크를 해제하면 이번 실행에서 완전히 제외됩니다. "
    "기간·provider 비율은 collection.yaml 기본값을 자동으로 쓰고, 필요한 LV2만 아래에서 개별로 바꾸세요."
)


def _validate_date_range(label: str, d_from, d_to) -> str | None:
    if d_to < d_from:
        return f"{label}: 종료일이 시작일보다 빠릅니다."
    if not allow_future and d_to > date.today():
        return f"{label}: 종료일은 오늘보다 미래일 수 없습니다 (configs/collection.yaml에서 변경 가능)."
    return None


date_error = None

for lv2_id in setup["selected_lv2"]:
    group = next(g for g in groups if g["lv2_id"] == lv2_id)
    with st.expander(f"{lv1_badge(group['lv1_name'])} › **{group['lv2_name']}** ({lv2_id})", expanded=True):
        st.caption(group["lv2_description"])

        col_a, col_b = st.columns(2)
        with col_a:
            lv2_default_range = default_date_range_for_lv2(configs, lv2_id) or (
                setup["date_from"], setup["date_to"]
            )
            use_custom_dates = st.checkbox(
                f"이 LV2만 기간 직접 조정 (기본값: {lv2_default_range[0]} ~ {lv2_default_range[1]})",
                value=lv2_id in setup["date_overrides"], key=f"custom_dates_{lv2_id}",
            )
            if use_custom_dates:
                prev = setup["date_overrides"].get(lv2_id, {})
                d_from = st.date_input(
                    "시작일", value=prev.get("date_from", lv2_default_range[0]), key=f"df_{lv2_id}",
                )
                d_to = st.date_input(
                    "종료일", value=prev.get("date_to", lv2_default_range[1]), key=f"dt_{lv2_id}",
                )
                setup["date_overrides"][lv2_id] = {"date_from": d_from, "date_to": d_to}
                date_error = date_error or _validate_date_range(lv2_id, d_from, d_to)
            else:
                setup["date_overrides"].pop(lv2_id, None)
        with col_b:
            by_lv2_default = configs["collection"]["provider_ratio"].get("by_lv2", {}).get(lv2_id)
            default_ratio = by_lv2_default or setup["provider_ratio"]

            use_custom_ratio = st.checkbox(
                f"이 LV2만 비율 직접 조정 (기본값: Tavily {default_ratio['tavily']}:"
                f"{default_ratio['serpapi']} SerpAPI)",
                value=lv2_id in setup["provider_ratio_overrides"], key=f"custom_ratio_{lv2_id}",
            )
            if use_custom_ratio:
                prev_pct = setup["provider_ratio_overrides"].get(lv2_id, default_ratio)["tavily"]
                lv2_tavily_pct = st.slider(
                    "Tavily 비율 (%)", min_value=0, max_value=100, value=prev_pct, key=f"ratio_{lv2_id}",
                )
                setup["provider_ratio_overrides"][lv2_id] = {
                    "tavily": lv2_tavily_pct, "serpapi": 100 - lv2_tavily_pct,
                }
            else:
                setup["provider_ratio_overrides"].pop(lv2_id, None)

        for t in group["types"]:
            key = type_key(lv2_id, t["name"])
            existing = counts.get((lv2_id, t["name"]), 0)
            with st.container(border=True):
                col_check, col_count = st.columns([4, 1])
                with col_check:
                    enabled = st.checkbox(
                        t["name"], value=setup["type_enabled"].get(key, True), key=f"chk_{key}",
                    )
                    st.caption(t["description"])
                    setup["type_enabled"][key] = enabled
                with col_count:
                    st.metric("보유", existing)

active_types = [
    (lv2_id, t) for lv2_id in setup["selected_lv2"]
    for group in [next(g for g in groups if g["lv2_id"] == lv2_id)]
    for t in [tt["name"] for tt in group["types"]]
    if setup["type_enabled"].get(type_key(lv2_id, t), True)
]
if not active_types:
    st.warning("활성화된 type이 없습니다. 최소 1개는 켜야 다음 단계로 진행할 수 있습니다.")
if date_error:
    st.error(date_error)

# ---------------------------------------------------------------- ③ 목표 수집량과 후보 배수
st.subheader("③ 목표 수집량과 후보 배수")
col1, col2 = st.columns(2)
with col1:
    setup["target_count"] = st.number_input(
        "LV2당 신규 목표 수집량 (target_count)",
        min_value=1, value=setup["target_count"],
        help=(
            "이미 DB에 쌓인 accepted 건수와 별개로, 이번 실행에서 새로 채우려는 목표치입니다 (7.2절). "
            "같은 LV2에 활성 type이 여럿이면 이 값을 type 수만큼 나눠 가집니다(나머지는 올림)."
        ),
    )
with col2:
    multiplier_help = {
        1.0: "비용 최소 — 딱 목표치만큼만 후보를 찾습니다. 필터링 후 목표 미달 위험이 가장 큽니다.",
        1.5: "기본값 — 필터링으로 걸러질 것을 감안한 무난한 여유분입니다.",
        2.0: "목표 달성 가능성 우선 — API 비용이 더 들지만 목표 미달 가능성이 낮아집니다.",
        3.0: "넉넉한 후보 확보 — 필터링이 까다로운 type에 적합하지만 비용이 가장 큽니다.",
    }
    options = configs["collection"]["candidate_multiplier_options"]
    setup["candidate_multiplier"] = st.select_slider(
        "후보 배수 (candidate_multiplier)", options=options, value=setup["candidate_multiplier"],
        help="candidate_target = ceil(목표 × 배수). 필터링으로 제외될 걸 감안해 더 많이 찾아봅니다 (7.1절).",
    )
    st.caption(multiplier_help.get(setup["candidate_multiplier"], ""))

st.caption("LV2별로 실제 찾아볼 후보 수 (target_count를 활성 type 수로 나눈 뒤 후보 배수 적용)")
types_per_lv2: dict[str, int] = {}
for lv2_id, t in active_types:
    types_per_lv2[lv2_id] = types_per_lv2.get(lv2_id, 0) + 1
st.dataframe(
    [
        {
            "LV2": lv2_id, "활성 type 수": n,
            "type당 목표": per_type_target_count(setup["target_count"], n),
            "type당 후보 수": candidate_target(
                per_type_target_count(setup["target_count"], n), setup["candidate_multiplier"],
            ),
        }
        for lv2_id, n in types_per_lv2.items()
    ],
    hide_index=True, width="stretch",
)

st.markdown("**API 크레딧 사용량** (7.7절)")
serpapi_key = os.environ.get(configs["providers"]["serpapi"]["api_key_env"])
if serpapi_key:
    usage = get_serpapi_usage(serpapi_key)
    if usage:
        col1, col2, col3 = st.columns(3)
        col1.metric("SerpAPI 이번 달 사용", usage.get("this_month_usage", "?"))
        col2.metric("SerpAPI 남은 검색(플랜)", usage.get("plan_searches_left", "?"))
        col3.metric("SerpAPI 남은 검색(전체)", usage.get("total_searches_left", "?"))
    else:
        st.caption("SerpAPI 사용량을 불러오지 못했습니다 (key 또는 네트워크를 확인해주세요).")
else:
    st.caption("SerpAPI key가 없어 사용량을 조회할 수 없습니다.")
st.caption("Tavily는 SDK에 사전 잔여 크레딧 조회 기능이 없어, 실행 후 '4. 실행 및 결과' 화면에서 실제 사용량만 확인할 수 있습니다.")

# ---------------------------------------------------------------- 확정
st.divider()
can_confirm = bool(active_types) and date_error is None
if st.button("✅ 설정 확정하고 다음 단계(도메인 설정)로", type="primary", disabled=not can_confirm):
    setup["confirmed"] = True
    st.success(f"설정을 확정했습니다. 활성 type {len(active_types)}개, 왼쪽 메뉴에서 '2. 도메인 설정'으로 이동하세요.")

if setup.get("confirmed"):
    with st.expander("현재 확정된 설정 요약", expanded=False):
        st.json({
            "activated_types": [f"{lv2}::{t}" for lv2, t in active_types],
            "target_count": setup["target_count"],
            "candidate_multiplier": setup["candidate_multiplier"],
            "date_overrides": {
                lv2: {"date_from": str(v["date_from"]), "date_to": str(v["date_to"])}
                for lv2, v in setup["date_overrides"].items()
            },
            "provider_ratio_overrides": setup["provider_ratio_overrides"],
        })
