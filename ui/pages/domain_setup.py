"""2단계: 도메인 설정 (14.2절) — type별 SerpAPI 허용 도메인 + 공통 블랙리스트."""

from __future__ import annotations

import streamlit as st

from src.discovery.allocator import serpapi_allowed_domains, tavily_exclude_domains
from ui.common import get_configs, get_db, save_yaml, selected_types, taxonomy_groups, type_option_label

st.title("🌐 2. 도메인 설정")
st.caption(
    "SerpAPI는 여기서 등록한 도메인 안에서만 검색합니다. "
    "Tavily는 공통 블랙리스트만 제외하며, 두 provider가 발견한 동일 URL은 수집 단계에서 중복 제거합니다."
)

configs = get_configs()
conn = get_db()


def _lines_to_domains(text: str) -> list[str]:
    return sorted({line.strip() for line in text.splitlines() if line.strip()})


# ---------------------------------------------------------------- 공통 블랙리스트
st.subheader("① 공통 블랙리스트 (모든 type · 양쪽 provider 공통 제외)")
blacklist_domains = configs["blacklist"]["domains"]
blacklist_text = st.text_area(
    "도메인 한 줄에 하나씩 입력하세요.",
    value="\n".join(blacklist_domains), height=120,
    help="예: youtube.com — 본문 추출이 안 되거나 한국어 콘텐츠 가능성이 거의 없는 도메인을 넣습니다.",
)
if st.button("블랙리스트 저장"):
    save_yaml("blacklist", {"domains": _lines_to_domains(blacklist_text)})
    st.success("블랙리스트를 저장했습니다.")
    st.rerun()

st.divider()

# ---------------------------------------------------------------- type별 SerpAPI 허용 도메인
st.subheader("② type별 SerpAPI 허용 도메인")

active = selected_types(configs)
if active:
    type_options = [f"{lv2}::{name}" for lv2, name in active]
    st.caption("1단계에서 활성화한 type만 보여줍니다.")
else:
    type_options = [
        f"{g['lv2_id']}::{t['name']}" for g in taxonomy_groups(configs) for t in g["types"]
    ]
    st.caption("1단계 설정을 먼저 확정하면 활성화한 type만 골라 보여줍니다. 지금은 전체 type을 보여줍니다.")

picked = st.selectbox(
    "type 선택", options=type_options,
    format_func=lambda opt: type_option_label(configs, *opt.split("::", 1)),
)
lv2_id, type_name = picked.split("::", 1)

type_domains_cfg = configs["type_domains"]["types"]
current_domains = serpapi_allowed_domains(type_domains_cfg, type_name, lv2_id=lv2_id)

domain_text = st.text_area(
    f"'{type_name}'의 SerpAPI 허용 도메인 (한 줄에 하나씩)",
    value="\n".join(current_domains), height=150,
)
if st.button("이 type의 도메인 저장"):
    type_domains_cfg.setdefault(lv2_id, {})[type_name] = {
        "serpapi_allowed_domains": _lines_to_domains(domain_text)
    }
    save_yaml("type_domains", {"types": type_domains_cfg})
    st.success(f"'{type_name}' 도메인을 저장했습니다.")
    st.rerun()

partition_cfg = configs["collection"].get("domain_partition", {})
preview = tavily_exclude_domains(
    type_domains_cfg,
    blacklist_domains,
    type_name,
    exclude_serpapi_domains=partition_cfg.get("tavily_excludes_serpapi_domains", False),
    lv2_id=lv2_id,
)
st.caption(f"➡️ Tavily가 '{type_name}' 검색 시 자동으로 제외할 도메인 ({len(preview)}개)")
st.code("\n".join(preview) if preview else "(없음)", language=None)
st.caption("SerpAPI 허용 도메인은 Tavily에서도 검색되며, 동일 URL은 중복 저장되지 않습니다.")

st.divider()

# ---------------------------------------------------------------- 도메인 누락 경고
st.subheader("③ 도메인 미등록 type (SerpAPI 없이 Tavily로만 진행)")
missing = [
    (lv2, name) for lv2, name in (active or [
        (g["lv2_id"], t["name"]) for g in taxonomy_groups(configs) for t in g["types"]
    ])
    if not serpapi_allowed_domains(type_domains_cfg, name, lv2_id=lv2)
]
if missing:
    st.warning(
        "아래 type은 SerpAPI 허용 도메인이 없어 SerpAPI 검색을 건너뛰고, "
        "해당 몫까지 전부 Tavily로 이관해 진행됩니다 (6.3절). 실행 전에 확인해주세요."
    )
    st.dataframe(
        [{"LV2": lv2, "type": name} for lv2, name in missing],
        hide_index=True, width="stretch",
    )
else:
    st.success("모든 대상 type에 SerpAPI 도메인이 등록돼 있습니다.")
