"""5단계: DB 데이터 탐색 — 실행과 무관하게, 지금까지 쌓인 DB 전체를 살펴본다."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from src.storage.repositories import domain_stats as domain_stats_repo
from src.storage.repositories import query_executions as exec_repo
from src.storage.repositories import query_generation_calls as generation_calls_repo
from src.storage.repositories import runs as runs_repo
from src.storage.repositories import taxonomy_mappings as mappings_repo
from ui.common import (
    accepted_counts, exclusion_reason_label, get_configs, get_db, groups_by_lv1, lv1_badge,
    openai_filter_reason_note, render_content_box, taxonomy_groups,
)

st.title("📊 5. 데이터 탐색")
st.caption("실행 결과가 아니라 DB에 지금까지 쌓인 전체 콘텐츠를 텍소노미별로 비교하고 개별 콘텐츠를 확인합니다.")

configs = get_configs()
conn = get_db()
groups = taxonomy_groups(configs)

# ---------------------------------------------------------------- 💰 DB 전체 API 사용량
st.subheader("💰 DB 전체 API 사용량")
st.caption("이 DB에 지금까지 쌓인 모든 실행을 합친 총 사용량입니다 (개별 실행 내역은 '4. 실행 및 결과'에서 확인).")

exec_calls = exec_repo.count_by_provider(conn)  # tavily/serpapi: 캐시 제외한 실제 호출 수 (정확)
gen_usage = generation_calls_repo.sum_usage(conn)  # openai: 검색어 생성
openai_filter_usage = runs_repo.sum_openai_filter_usage(conn)  # openai: openai_filter

openai_cfg = configs["providers"]["openai"]
total_prompt_tokens = gen_usage.prompt_tokens + openai_filter_usage["prompt_tokens"]
total_completion_tokens = gen_usage.completion_tokens + openai_filter_usage["completion_tokens"]
total_openai_calls = gen_usage.calls + openai_filter_usage["calls"]
total_openai_cost = (
    total_prompt_tokens * openai_cfg["input_price_per_1m_usd"]
    + total_completion_tokens * openai_cfg["output_price_per_1m_usd"]
) / 1_000_000

col1, col2, col3 = st.columns(3)
col1.metric("tavily 총 호출 수", exec_calls.get("tavily", 0))
col2.metric("serpapi 총 호출 수", exec_calls.get("serpapi", 0))
col3.metric("openai 총 호출 수", total_openai_calls, help="검색어 생성 + openai_filter 합산")

col4, col5 = st.columns(2)
col4.metric("openai 총 토큰(입력+출력)", total_prompt_tokens + total_completion_tokens)
col5.metric("openai 총 예상 비용", f"${total_openai_cost:.4f}")

st.dataframe(
    [
        {
            "용도": "검색어 생성", "호출 수": gen_usage.calls,
            "토큰(입력/출력)": f"{gen_usage.prompt_tokens}/{gen_usage.completion_tokens}",
            "소요시간": f"{gen_usage.elapsed_s:.1f}s",
        },
        {
            "용도": "openai_filter", "호출 수": openai_filter_usage["calls"],
            "토큰(입력/출력)": f"{openai_filter_usage['prompt_tokens']}/{openai_filter_usage['completion_tokens']}",
            "소요시간": f"{openai_filter_usage['elapsed_s']:.1f}s",
        },
    ],
    hide_index=True, width="stretch",
)

st.divider()

# ---------------------------------------------------------------- ① 텍소노미별 수집량 비교
st.subheader("① LV2별 accepted 수집량")

counts = accepted_counts(conn)  # (lv2, type) -> count
counts_by_lv2: dict[str, int] = {}
for (lv2, _type), n in counts.items():
    counts_by_lv2[lv2] = counts_by_lv2.get(lv2, 0) + n

chart_df = pd.DataFrame(
    [
        {
            "label": f"{'_'.join(g['lv2_id'].split('_')[:2])} · {g['lv2_name']}",
            "count": counts_by_lv2.get(g["lv2_id"], 0),
        }
        for g in groups
    ]
)
# 막대 차트 자체는 Altair(streamlit 내장 의존성)로 그려서, y축 라벨 폭 제한(labelLimit=0)으로
# 텍소노미 이름이 길어도 잘리지 않게 한다. 라벨은 축 영역에 그려지므로 막대와 겹치지 않는다.
chart = (
    alt.Chart(chart_df)
    .mark_bar()
    .encode(
        x=alt.X("count:Q", title="accepted 수"),
        y=alt.Y("label:N", sort=None, title=None, axis=alt.Axis(labelLimit=0)),
    )
    .properties(height=max(200, 32 * len(chart_df)))
)
st.altair_chart(chart, use_container_width=True)

st.metric("전체 accepted 수", sum(counts_by_lv2.values()))

st.divider()

# ---------------------------------------------------------------- ①-2 도메인별 추출 성공률
st.subheader("① -2 도메인별 추출 성공률")
st.caption(
    "새 도메인을 type_domains.yaml/blacklist.yaml에 넣을지는 여기 실측치로 판단합니다 "
    "(추출 실패율이 높으면 정적 렌더링이 안 되는 사이트일 가능성이 큽니다)."
)
domain_stats = domain_stats_repo.list_all_domain_stats(conn)
if not domain_stats:
    st.caption("아직 시도된 도메인이 없습니다.")
else:
    domain_table = sorted(
        (
            {
                "도메인": d.domain, "성공(저장)": d.success, "추출 실패": d.extraction_failed,
                "성공률": f"{d.success_rate:.0%}" if d.success_rate is not None else "-",
                "_rate": d.success_rate if d.success_rate is not None else 1.0,
            }
            for d in domain_stats
        ),
        key=lambda r: r["_rate"],
    )
    for r in domain_table:
        del r["_rate"]
    st.dataframe(domain_table, hide_index=True, width="stretch")

st.divider()

# ---------------------------------------------------------------- ② 필터
st.subheader("② 콘텐츠 목록")

col1, col2, col3 = st.columns(3)
with col1:
    lv1_options = ["전체"] + list(groups_by_lv1(configs).keys())
    picked_lv1 = st.selectbox("LV1", lv1_options)

lv2_candidates = [g for g in groups if picked_lv1 == "전체" or g["lv1_name"] == picked_lv1]
with col2:
    lv2_options = ["전체"] + [f"{g['lv2_name']} ({g['lv2_id']})" for g in lv2_candidates]
    picked_lv2_label = st.selectbox("LV2", lv2_options)
picked_lv2 = None if picked_lv2_label == "전체" else picked_lv2_label.split("(")[-1].rstrip(")")

type_candidates = (
    [t["name"] for g in lv2_candidates for t in g["types"] if picked_lv2 is None or g["lv2_id"] == picked_lv2]
)
with col3:
    type_options = ["전체"] + sorted(set(type_candidates))
    picked_type_label = st.selectbox("type", type_options)
picked_type = None if picked_type_label == "전체" else picked_type_label

status_label = st.radio("상태", ["전체", "accepted", "excluded"], horizontal=True)
picked_status = None if status_label == "전체" else status_label

rows = mappings_repo.list_with_content(
    conn, taxonomy_lv2=picked_lv2, type_name=picked_type, decision=picked_status,
)
st.caption(f"{len(rows)}건")

# ---------------------------------------------------------------- ③ 표 (체크해서 본문 확인)
if not rows:
    st.info("조건에 맞는 콘텐츠가 없습니다.")
else:
    table = [
        {
            "기사 제목": r["title"],
            "텍소노미": r["taxonomy_lv2"],
            "type": r["type_name"],
            "상태": r["decision"] or r["status"],
            "제외 사유": exclusion_reason_label(r["decision"], r["decision_reason"]),
            "openai 필터 판단 근거": openai_filter_reason_note(r["decision_reason"]),
            "원문 게시일": r["published_date"] or "",
            "수집 출처 사이트": r["source_domain"],
            "provider": r["provider"] or "",
            "URL": r["canonical_url"],
        }
        for r in rows
    ]
    event = st.dataframe(
        table, hide_index=True, width="stretch",
        on_select="rerun", selection_mode="single-row",
        column_config={"URL": st.column_config.LinkColumn(display_text="열기")},
    )

    selected_idx = event.selection.rows if event and event.selection else []
    for i in selected_idx:
        r = rows[i]
        with st.expander(f"📄 {r['title']}", expanded=True):
            col_a, col_b = st.columns(2)
            col_a.metric("상태", r["decision"] or r["status"])
            col_b.metric("provider", r["provider"] or "-")
            st.caption(f"{r['taxonomy_lv2']} · {r['type_name']} · {r['source_domain']} · {r['canonical_url']}")
            if r["decision_reason"]:
                st.info(r["decision_reason"])
            render_content_box(r["content"])
