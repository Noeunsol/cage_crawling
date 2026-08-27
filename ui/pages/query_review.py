"""3단계: 검색어 생성 및 검토 (14.3절).

① 대상 type을 LV2별로 묶어 접이식으로 보여주고 (이미 검색어가 있으면 기본 체크 해제 — 재사용),
   필요한 type만 골라 provider별 개수를 정해 한 번에 생성한다.
② type별로 검색어를 검토/수정/거부/직접 추가한다 — 이것도 LV2별로 묶여 있다.
LV2가 여러 개 선택되면 목록이 길어지므로, 두 섹션 모두 LV2 단위로 접었다 펼 수 있게 한다.
"""

from __future__ import annotations

import streamlit as st

from src.query import generator, repository
from src.storage.repositories import query_executions as exec_repo
from src.storage.repositories import query_generation_calls as generation_calls_repo
from src.utils.prompts import load_prompt
from ui.common import (
    find_type, get_configs, get_db, lv1_badge, selected_types, show_missing_api_key_banner, suggested_query_counts,
    taxonomy_groups,
)

st.title("🔍 3. 검색어 생성 및 검토")
st.caption("GPT-4o-mini가 provider별로 검색어를 만들면, 여기서 그대로 쓸지/고칠지/뺄지 정합니다. 실제 검색은 이 화면 이후에만 실행됩니다.")

configs = get_configs()
conn = get_db()
show_missing_api_key_banner(configs["providers"])

active = selected_types(configs)
if active:
    targets = active
    st.caption("1단계에서 활성화한 type 전체가 대상입니다.")
else:
    targets = [(g["lv2_id"], t["name"]) for g in taxonomy_groups(configs) for t in g["types"]]
    st.caption("1단계 설정을 먼저 확정하면 활성화한 type만 대상이 됩니다. 지금은 전체 type이 대상입니다.")

setup = st.session_state.get("setup")
groups = taxonomy_groups(configs)

# LV2별로 묶는다 — targets는 이미 taxonomy 순서대로 LV2가 뭉쳐 있어 이대로 dict에 넣으면 순서가 유지된다.
types_by_lv2: dict[str, list[str]] = {}
for lv2, type_name in targets:
    types_by_lv2.setdefault(lv2, []).append(type_name)


def _lv2_header(lv2: str) -> str:
    group = next(g for g in groups if g["lv2_id"] == lv2)
    return f"{lv1_badge(group['lv1_name'])} › **{group['lv2_name']}** ({lv2})"


def _active_counts(lv2: str, type_name: str) -> tuple[int, int]:
    tavily_n = len(repository.list_active_queries(conn, taxonomy_lv2=lv2, type_name=type_name, provider="tavily"))
    serpapi_n = len(repository.list_active_queries(conn, taxonomy_lv2=lv2, type_name=type_name, provider="serpapi"))
    return tavily_n, serpapi_n


# ---------------------------------------------------------------- ① 일괄 생성 대상 선택
st.subheader("① 검색어 생성 대상 선택")
st.caption(
    "이미 검색어가 있는 type은 재사용하도록 기본 체크 해제됩니다. 필요한 type·개수만 골라 한 번에 생성하세요. "
    + (
        "기본 개수는 1단계 수집 설정(목표량·후보 배수·provider 비율)으로 역산한 제안값입니다."
        if setup else
        "1단계 수집 설정을 먼저 확정하면 목표량 기반으로 개수를 제안해줍니다. 지금은 고정 기본값입니다."
    )
)

plan = st.session_state.setdefault("query_gen_plan", {})

for lv2, type_names in types_by_lv2.items():
    for type_name in type_names:
        key = f"{lv2}::{type_name}"
        if key not in plan:
            tavily_n, serpapi_n = _active_counts(lv2, type_name)
            # type마다 target(수집 설정 기반)과 보유 개수가 다르므로, 이미 보유한 만큼은 빼고
            # 부족한 만큼만 생성 제안한다 — 안 그러면 이미 충분한 type도 매번 새로 더 만들게 된다.
            target_tavily, target_serpapi = suggested_query_counts(configs, setup, lv2, len(type_names))
            plan[key] = {
                "checked": tavily_n == 0 or serpapi_n == 0,
                "tavily_count": max(0, target_tavily - tavily_n),
                "serpapi_count": max(0, target_serpapi - serpapi_n),
            }

total_calls = 0
for lv2, type_names in types_by_lv2.items():
    picked = sum(1 for t in type_names if plan[f"{lv2}::{t}"]["checked"])
    with st.expander(f"{_lv2_header(lv2)}  ·  {picked}/{len(type_names)}개 선택됨", expanded=picked > 0):
        col_a, col_b = st.columns(2)
        if col_a.button("이 LV2 전체 선택", key=f"select_all_{lv2}", width="stretch"):
            for t in type_names:
                st.session_state[f"gen_pick_{lv2}::{t}"] = True
            st.rerun()
        if col_b.button("이 LV2 전체 해제", key=f"deselect_all_{lv2}", width="stretch"):
            for t in type_names:
                st.session_state[f"gen_pick_{lv2}::{t}"] = False
            st.rerun()

        for type_name in type_names:
            key = f"{lv2}::{type_name}"
            tavily_n, serpapi_n = _active_counts(lv2, type_name)
            target_tavily, target_serpapi = suggested_query_counts(configs, setup, lv2, len(type_names))
            with st.container(border=True):
                col_check, col_tavily, col_serpapi = st.columns([3, 1, 1])
                with col_check:
                    plan[key]["checked"] = st.checkbox(
                        f"{type_name}  (보유: tavily {tavily_n} · serpapi {serpapi_n} "
                        f"/ 목표: tavily {target_tavily} · serpapi {target_serpapi})",
                        value=plan[key]["checked"], key=f"gen_pick_{key}",
                    )
                with col_tavily:
                    plan[key]["tavily_count"] = st.number_input(
                        "tavily 개수", min_value=0, value=plan[key]["tavily_count"], key=f"tc_{key}",
                        help=f"목표 {target_tavily}개 - 보유 {tavily_n}개 = 제안 {max(0, target_tavily - tavily_n)}개",
                    )
                with col_serpapi:
                    plan[key]["serpapi_count"] = st.number_input(
                        "serpapi 개수", min_value=0, value=plan[key]["serpapi_count"], key=f"sc_{key}",
                        help=f"목표 {target_serpapi}개 - 보유 {serpapi_n}개 = 제안 {max(0, target_serpapi - serpapi_n)}개",
                    )

            if plan[key]["checked"]:
                total_calls += (plan[key]["tavily_count"] > 0) + (plan[key]["serpapi_count"] > 0)

st.metric("이번 일괄 생성으로 예상되는 OpenAI 호출 수", total_calls)

if st.button("✨ 선택한 type 일괄 생성", type="primary", disabled=total_calls == 0):
    try:
        client = generator.build_client(configs["providers"])
    except RuntimeError as e:
        st.error(str(e))
    else:
        prompt_cfg = load_prompt("query_generation")
        model = configs["providers"]["openai"]["model"]
        openai_cfg = configs["providers"]["openai"]
        generated_summary = []
        rejected_summary = []
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_elapsed_s = 0.0

        for lv2, type_name in targets:
            key = f"{lv2}::{type_name}"
            if not plan[key]["checked"]:
                continue
            type_cfg = find_type(configs, lv2, type_name)
            for provider, count in [
                ("tavily", plan[key]["tavily_count"]), ("serpapi", plan[key]["serpapi_count"]),
            ]:
                if count <= 0:
                    continue
                result = generator.generate_queries(
                    client, prompt_cfg, taxonomy_lv2=lv2, type_name=type_name,
                    definition=type_cfg["definition"],
                    search_vocabulary=type_cfg.get("search_vocabulary", []),
                    include_criteria=type_cfg["include_criteria"],
                    exclude_criteria=type_cfg["exclude_criteria"], provider=provider,
                    query_count=int(count), model=model,
                )
                repository.save_generated_queries(
                    conn, taxonomy_lv2=lv2, type_name=type_name, provider=provider,
                    query_texts=result.accepted, prompt_version=str(prompt_cfg["version"]), model=model,
                )
                total_prompt_tokens += result.prompt_tokens
                total_completion_tokens += result.completion_tokens
                total_elapsed_s += result.elapsed_s
                generation_calls_repo.record_call(
                    conn, taxonomy_lv2=lv2, type_name=type_name, provider=provider, model=model,
                    prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                    elapsed_s=result.elapsed_s,
                )
                generated_summary.append({
                    "LV2": lv2, "type": type_name, "provider": provider, "생성됨": len(result.accepted),
                    "토큰(입력/출력)": f"{result.prompt_tokens}/{result.completion_tokens}",
                    "소요시간": f"{result.elapsed_s:.1f}s",
                })
                for text, reason in result.rejected:
                    rejected_summary.append({"type": type_name, "provider": provider, "사유": reason, "검색어": text})

        total_cost_usd = (
            total_prompt_tokens * openai_cfg["input_price_per_1m_usd"]
            + total_completion_tokens * openai_cfg["output_price_per_1m_usd"]
        ) / 1_000_000

        st.session_state["last_query_gen_result"] = {
            "generated_summary": generated_summary, "rejected_summary": rejected_summary,
            "total_tokens": total_prompt_tokens + total_completion_tokens,
            "elapsed_s": total_elapsed_s, "cost_usd": total_cost_usd,
        }
        st.rerun()

last_result = st.session_state.get("last_query_gen_result")
if last_result:
    st.success(f"{len(last_result['generated_summary'])}개 provider 조합에 대해 생성을 마쳤습니다.")
    col_tok, col_time, col_cost = st.columns(3)
    col_tok.metric("총 토큰(입력+출력)", last_result["total_tokens"])
    col_time.metric("총 소요시간", f"{last_result['elapsed_s']:.1f}s")
    col_cost.metric("예상 비용", f"${last_result['cost_usd']:.4f}")
    if last_result["generated_summary"]:
        st.dataframe(last_result["generated_summary"], hide_index=True, width="stretch")
    if last_result["rejected_summary"]:
        with st.expander(f"⚠️ 규칙 위반으로 자동 제외된 검색어 ({len(last_result['rejected_summary'])}개)"):
            st.dataframe(last_result["rejected_summary"], hide_index=True, width="stretch")

st.divider()

# ---------------------------------------------------------------- ② type별 검토
st.subheader("② type별 검토 및 직접 추가")


def _render_provider_detail(lv2: str, type_name: str, provider: str) -> None:
    st.markdown(f"**{provider.upper()}**")
    rows = repository.list_active_queries(conn, taxonomy_lv2=lv2, type_name=type_name, provider=provider)
    if not rows:
        st.caption("검색어가 없습니다.")

    for row in rows:
        c1, c2, c3 = st.columns([5, 1, 1])
        new_text = c1.text_input(
            "query", value=row["query_text"], key=f"text_{row['id']}", label_visibility="collapsed",
        )
        if c2.button("수정", key=f"save_{row['id']}"):
            if new_text.strip() and new_text != row["query_text"]:
                repository.edit_query(conn, row["id"], new_text.strip())
                st.rerun()
            else:
                st.info("바뀐 내용이 없습니다.")
        if c3.button("거부", key=f"reject_{row['id']}"):
            repository.reject_query(conn, row["id"])
            st.rerun()

    new_query = st.text_input(
        f"{provider} 직접 추가", key=f"manual_{lv2}_{type_name}_{provider}",
        label_visibility="collapsed", placeholder=f"{provider} 검색어 직접 입력",
    )
    if st.button("+ 추가", key=f"add_{lv2}_{type_name}_{provider}") and new_query.strip():
        repository.add_manual_query(
            conn, taxonomy_lv2=lv2, type_name=type_name, provider=provider, query_text=new_query.strip(),
        )
        st.rerun()

    used_rows = repository.list_used_queries(conn, taxonomy_lv2=lv2, type_name=type_name, provider=provider)
    if used_rows:
        with st.expander(f"이미 사용됨 ({len(used_rows)}개)"):
            st.caption(
                "실제 검색에 쓰인 검색어입니다. 그 실행이 도중에 죽어서 결과가 안 남았다면, "
                "되돌리기를 눌러야 다음 실행에서 진짜로 다시 검색됩니다 (그냥 두면 '이미 검색했다'고 보고 건너뜁니다). "
                "결과 수가 0건인 검색어는 그 표현으로는 못 찾는다는 뜻이니, 되돌린 뒤 검색어 자체를 바꿔보세요."
            )
            for row in used_rows:
                result_count = exec_repo.get_latest_result_count(conn, row["id"])
                count_label = "실행 기록 없음" if result_count is None else f"{result_count}건"
                c1, c2, c3 = st.columns([5, 1, 1])
                c1.caption(row["query_text"])
                if result_count == 0:
                    c2.markdown("⚠️ **0건**")
                else:
                    c2.caption(count_label)
                if c3.button("되돌리기", key=f"reactivate_{row['id']}"):
                    repository.reactivate_query(conn, row["id"])
                    st.rerun()


empty_types = []
for lv2, type_names in types_by_lv2.items():
    with st.expander(_lv2_header(lv2)):
        for type_name in type_names:
            tavily_n, serpapi_n = _active_counts(lv2, type_name)
            if tavily_n + serpapi_n == 0:
                empty_types.append((lv2, type_name))

            target_tavily, target_serpapi = suggested_query_counts(configs, setup, lv2, len(type_names))
            type_cfg = find_type(configs, lv2, type_name)
            with st.container(border=True):
                st.markdown(
                    f"**{type_name}**  (보유: tavily {tavily_n} · serpapi {serpapi_n} "
                    f"/ 목표: tavily {target_tavily} · serpapi {target_serpapi})"
                )
                st.caption(type_cfg["definition"])
                col_tavily, col_serpapi = st.columns(2)
                with col_tavily:
                    _render_provider_detail(lv2, type_name, "tavily")
                with col_serpapi:
                    _render_provider_detail(lv2, type_name, "serpapi")

st.divider()

# ---------------------------------------------------------------- 검색어 0개 type 경고
st.subheader("검색어 미확보 type")
if empty_types:
    st.warning("아래 type은 사용할 검색어가 하나도 없어 이번 실행에서 자동으로 제외됩니다 (5.5절).")
    st.dataframe([{"LV2": lv2, "type": name} for lv2, name in empty_types], hide_index=True, width="stretch")
else:
    st.success("모든 대상 type에 검색어가 최소 1개 이상 있습니다.")
