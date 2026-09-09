"""4단계: 사전 검사 → 실행 → 결과. 실제 API를 호출하는 유일한 화면이다."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import streamlit as st

from src.discovery.serpapi_provider import SerpApiProvider
from src.discovery.tavily_provider import TavilyProvider
from src.pipeline.collector import run_collection
from src.pipeline.preflight import run_preflight
from src.query.generator import build_client
from src.storage.csv_exporter import export_run
from src.storage.repositories import discarded as discarded_repo
from src.storage.repositories import discoveries as discoveries_repo
from src.storage.repositories import runs as runs_repo
from ui.common import (
    EXCLUSION_REASON_LABELS, PROJECT_ROOT, effective_date_range, effective_provider_ratio,
    exclusion_reason_label, get_configs, get_db, openai_filter_reason_note, render_content_box, selected_types,
    show_missing_api_key_banner,
)


def _korea_relevance_note(decision_reason: str | None) -> str:
    if not decision_reason or "한국 관련성:" not in decision_reason:
        return ""
    return decision_reason.split("한국 관련성:", 1)[1].strip()


def _provider_api_key(configs: dict, provider: str) -> str:
    return os.environ[configs["providers"][provider]["api_key_env"]]


_KST = timezone(timedelta(hours=9))


def _format_started_at(started_at: str) -> str:
    """DB에는 UTC로 저장되어 있으므로 표시 시 한국 시간(KST, UTC+9)으로 변환한다."""
    utc_dt = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(_KST).strftime("%Y-%m-%d %H:%M:%S")


def _auto_export_accepted(conn, configs: dict, run_id: str) -> None:
    """이 run에서 새로 accepted된 콘텐츠를 CSV에 자동 반영한다 (기존 CSV 유지, 중복 없이 누적).

    export_run() 자체가 멱등이라 실행 완료 시점에 매번 불러도 안전하다.
    실패해도 실행 결과 자체는 이미 DB에 저장돼 있으니, 여기 오류는 경고만 남기고 삼킨다 — 사용자는
    결과 화면의 "이 run을 CSV에 추가" 버튼으로 언제든 다시 시도할 수 있다.
    """
    try:
        final_dir = PROJECT_ROOT / configs["app"]["export"]["final_dir"]
        export_results = export_run(conn, run_id, final_dir)
    except Exception as e:
        st.warning(f"CSV 자동 내보내기에 실패했습니다 ({e}) — 결과 화면의 버튼으로 다시 시도할 수 있습니다.")
        return
    for result in export_results:
        st.caption(f"CSV 자동 반영: `{result.path.relative_to(PROJECT_ROOT)}` — 누적 {result.row_count}건")


def _format_elapsed(seconds: float) -> str:
    total_seconds = round(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _run_taxonomy_label(run_row) -> str:
    targets = json.loads(run_row["settings_snapshot"]).get("targets", [])
    lv2s = sorted({t.split("::")[0] for t in targets})
    return ", ".join(lv2s) if lv2s else "-"


def _render_run_results(conn, configs: dict, run_id: str) -> None:
    """DB에 저장된 값만으로 결과를 그린다 — 방금 끝난 실행이든 이전 실행 기록이든 동일하게 동작한다."""
    run_row = runs_repo.get_run(conn, run_id)
    if run_row is None:
        st.warning("실행 기록을 찾을 수 없습니다.")
        return

    rows = discoveries_repo.list_by_run(conn, run_id)
    discarded_rows = discarded_repo.list_by_run(conn, run_id)
    accepted = sum(1 for r in rows if r["decision"] == "accepted")
    excluded = sum(1 for r in rows if r["decision"] == "excluded")

    st.subheader(f"③ 결과 — {run_id} ({_format_started_at(run_row['started_at'])})")
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("찾은 후보", len(rows) + len(discarded_rows))
    col2.metric("accepted", accepted)
    col3.metric("excluded", excluded)
    col4.metric("discarded", len(discarded_rows))
    col5.metric("총 실행 시간", _format_elapsed(runs_repo.elapsed_seconds(run_row)))

    # excluded(본문은 저장됐지만 필터 탈락)와 discarded(본문 자체를 못 가져옴)를 하나의
    # 사유별 건수 표로 합친다 — 둘 다 같은 reason 코드 체계(retry_policy.yaml)를 쓴다.
    reason_counts: dict[str, int] = {}
    for r in discarded_rows:
        reason_counts[r["reason"]] = reason_counts.get(r["reason"], 0) + 1
    for r in rows:
        if r["decision"] == "excluded" and r["decision_reason"]:
            code = r["decision_reason"].split(":", 1)[0].strip()
            reason_counts[code] = reason_counts.get(code, 0) + 1

    if reason_counts:
        st.caption("제외 사유별 건수 (excluded + discarded 합산)")
        st.dataframe(
            [{"사유": EXCLUSION_REASON_LABELS.get(k, k), "건수": v} for k, v in reason_counts.items()],
            hide_index=True, width="stretch",
        )

    warnings = json.loads(run_row["warning_summary"]) if run_row["warning_summary"] else []
    for w in warnings:
        st.warning(w)

    st.caption(
        "실행이 끝나면 accepted 결과는 자동으로 CSV에 반영됩니다. 자동 반영이 실패했거나 "
        "이후 재분류 등으로 DB가 바뀌어 다시 맞추고 싶을 때만 아래 버튼을 누르세요 (중복 없이 다시 씁니다)."
    )
    if st.button("📄 이 run을 CSV에 다시 반영", key=f"export_csv_{run_id}"):
        _auto_export_accepted(conn, configs, run_id)

    provider_usage = json.loads(run_row["provider_usage_summary"]) if run_row["provider_usage_summary"] else {}
    st.caption("provider별 실제 API 호출 수 (캐시로 건너뛴 요청 제외)")
    st.dataframe(
        [{"provider": p, "호출 수": len(calls)} for p, calls in provider_usage.items()],
        hide_index=True, width="stretch",
    )

    openai_calls = provider_usage.get("openai", [])
    if openai_calls:
        openai_cfg = configs["providers"]["openai"]
        prompt_tokens = sum(c["prompt_tokens"] for c in openai_calls)
        completion_tokens = sum(c["completion_tokens"] for c in openai_calls)
        elapsed_s = sum(c["elapsed_s"] for c in openai_calls)
        cost_usd = (
            prompt_tokens * openai_cfg["input_price_per_1m_usd"]
            + completion_tokens * openai_cfg["output_price_per_1m_usd"]
        ) / 1_000_000
        st.caption("openai(openai_filter) 사용량")
        col_tok, col_time, col_cost = st.columns(3)
        col_tok.metric("총 토큰(입력+출력)", prompt_tokens + completion_tokens)
        col_time.metric("총 소요시간", f"{elapsed_s:.1f}s")
        col_cost.metric("예상 비용", f"${cost_usd:.4f}")

    # ------------------------------------------------------------ ④ 결과 목록 (체크해서 본문 확인)
    st.subheader("④ 결과 목록")
    if not rows:
        st.caption("이 실행에서 저장된 콘텐츠가 없습니다.")
        return

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
            "provider": r["provider"],
            "한국 적합성": _korea_relevance_note(r["decision_reason"]),
            "URL": r["canonical_url"],
        }
        for r in rows
    ]
    event = st.dataframe(
        table, hide_index=True, width="stretch",
        on_select="rerun", selection_mode="single-row", key=f"results_{run_id}",
        column_config={"URL": st.column_config.LinkColumn(display_text="열기")},
    )

    selected_idx = event.selection.rows if event and event.selection else []
    for i in selected_idx:
        r = rows[i]
        with st.expander(f"📄 {r['title']}", expanded=True):
            col_a, col_b = st.columns(2)
            col_a.metric("상태", r["decision"] or r["status"])
            col_b.metric("provider", r["provider"])
            st.caption(f"{r['taxonomy_lv2']} · {r['type_name']} · {r['source_domain']} · {r['canonical_url']}")
            if r["decision_reason"]:
                st.info(r["decision_reason"])
            render_content_box(r["content"])

st.title("🚀 4. 실행 및 결과")
st.caption("1단계에서 확정한 설정으로 실제 검색·수집을 실행합니다. 이 화면만 실제 API 비용이 발생합니다.")

configs = get_configs()
conn = get_db()
show_missing_api_key_banner(configs["providers"])

setup = st.session_state.get("setup")
if not setup or not setup.get("confirmed"):
    st.warning("먼저 '1. 수집 설정'에서 설정을 확정해주세요.")
    st.stop()

targets = selected_types(configs)
if not targets:
    st.warning("활성화된 type이 없습니다. '1. 수집 설정'을 확인해주세요.")
    st.stop()

date_range_by_lv2 = {lv2: effective_date_range(setup, configs, lv2) for lv2, _ in targets}
provider_ratio_by_lv2 = {lv2: effective_provider_ratio(setup, configs, lv2) for lv2, _ in targets}

# ---------------------------------------------------------------- 사전 검사
st.subheader("① 사전 검사")
report = run_preflight(
    conn, configs, targets,
    target_count=setup["target_count"], candidate_multiplier=setup["candidate_multiplier"],
)

if report.missing_api_keys:
    st.error("API key가 없어 실행할 수 없습니다:\n\n" + "\n".join(f"- {m}" for m in report.missing_api_keys))

st.dataframe(
    [
        {
            "LV2": t.lv2_id, "type": t.type_name, "후보 목표": t.candidate_target,
            "Tavily 검색어": t.tavily_query_count, "SerpAPI 검색어": t.serpapi_query_count,
            "SerpAPI 도메인": "있음" if t.has_serpapi_domain else "없음",
        }
        for t in report.type_reports
    ],
    hide_index=True, width="stretch",
)
tavily_est = report.max_requests_by_provider.get("tavily", 0)
serpapi_est = report.max_requests_by_provider.get("serpapi", 0)
st.caption(
    f"최악의 경우 이번 실행에서 최대 {report.max_requests_estimate:.0f}건의 검색 요청이 발생할 수 있습니다 "
    f"(tavily 최대 {tavily_est:.0f}건 / serpapi 최대 {serpapi_est:.0f}건)."
)

for w in report.domain_missing_warnings + report.no_query_warnings:
    st.warning(w)

# ---------------------------------------------------------------- 실행
st.subheader("② 실행")
use_openai_filter = st.checkbox(
    "openai 필터링 사용 (OpenAI 추가 호출)",
    value=configs["extraction"]["openai_filter"]["enabled"],
    help=(
        "켜면 광고성·위키형·일반정의·taxonomy 부적합 콘텐츠, 해외 사례까지 GPT-4o-mini로 한 번 더 "
        "0~4점 채점해서 걸러냅니다 (콘텐츠당 OpenAI 호출 1회 추가). 끄면 블랙리스트·중복·기간·"
        "한국 관련성(한글 비율)만 통과해도 채택됩니다."
    ),
)
limit_calls = st.checkbox(
    "이번 실행만 provider별 API 호출 수 상한 걸기 (실험용)",
    help="target_count 계산과 무관하게, 이번 실행에서 tavily/serpapi 실제 호출 수(캐시 적중 제외)를 직접 정합니다.",
)
max_calls_by_provider = None
if limit_calls:
    col_tavily_cap, col_serpapi_cap = st.columns(2)
    max_calls_by_provider = {
        "tavily": col_tavily_cap.number_input("tavily 최대 호출 수", min_value=0, value=10),
        "serpapi": col_serpapi_cap.number_input("serpapi 최대 호출 수", min_value=0, value=10),
    }

confirm_cost = st.checkbox("실제 API를 호출해 비용이 발생하는 것에 동의합니다.")
run_disabled = not report.can_run or not confirm_cost

if st.button("▶️ 실행 시작", type="primary", disabled=run_disabled):
    run_id = runs_repo.new_run_id()
    providers = {
        "tavily": TavilyProvider(_provider_api_key(configs, "tavily"), configs["providers"]["tavily"]),
        "serpapi": SerpApiProvider(_provider_api_key(configs, "serpapi"), configs["providers"]["serpapi"]),
    }
    openai_client = build_client(configs["providers"])
    run_configs = {
        **configs,
        "extraction": {**configs["extraction"], "openai_filter": {"enabled": use_openai_filter}},
    }

    runs_repo.create_run(conn, run_id, {
        "target_count": setup["target_count"], "candidate_multiplier": setup["candidate_multiplier"],
        "adaptive_multiplier": True,
        "targets": [f"{lv2}::{t}" for lv2, t in targets],
        "openai_filter_enabled": use_openai_filter,
        "max_calls_by_provider": max_calls_by_provider,
    })

    progress_bar = st.progress(0.0)
    status_line = st.empty()

    def _on_progress(event):
        progress_bar.progress(event.processed / max(event.total, 1))
        status_line.write(
            f"[{event.processed}/{event.total}] {event.lv2_id}::{event.type_name} "
            f"→ **{event.outcome.status}**"
            + (f" ({event.outcome.reason})" if event.outcome.reason else "")
        )

    try:
        with st.spinner("검색 및 수집 진행 중...", show_time=True):
            summary = run_collection(
                conn, providers, run_configs, run_id, targets,
                target_count=setup["target_count"], candidate_multiplier=setup["candidate_multiplier"],
                date_range_by_lv2=date_range_by_lv2, provider_ratio_by_lv2=provider_ratio_by_lv2,
                openai_client=openai_client, on_progress=_on_progress,
                max_calls_by_provider=max_calls_by_provider,
                use_adaptive_multiplier=True,
            )
    except Exception as error:
        runs_repo.finish_run(conn, run_id, "failed", {}, [f"실행 오류: {error}"])
        _auto_export_accepted(conn, configs, run_id)  # 실패 전까지 accepted된 것만이라도 CSV에 남긴다
        st.exception(error)
        st.stop()

    runs_repo.finish_run(conn, run_id, "completed", summary.provider_usage, summary.warnings)
    _auto_export_accepted(conn, configs, run_id)
    st.session_state["last_run_id"] = run_id
    st.success("실행이 끝났습니다. 아래에서 결과를 확인하세요.")

# ---------------------------------------------------------------- 결과 (방금 끝난 실행)
last_run_id = st.session_state.get("last_run_id")
if last_run_id:
    _render_run_results(conn, configs, last_run_id)

st.divider()

# ---------------------------------------------------------------- 실행 기록
st.subheader("⑤ 실행 기록")
past_runs = runs_repo.list_runs(conn)
if not past_runs:
    st.caption("아직 실행 기록이 없습니다.")
else:
    run_table = [
        {
            "실행 시각": _format_started_at(r["started_at"]),
            "run_id": r["run_id"],
            "상태": r["status"],
            "텍소노미": _run_taxonomy_label(r),
            "총 소요시간": _format_elapsed(runs_repo.elapsed_seconds(r)),
        }
        for r in past_runs
    ]
    history_event = st.dataframe(
        run_table, hide_index=True, width="stretch",
        on_select="rerun", selection_mode="single-row", key="run_history",
    )
    picked_rows = history_event.selection.rows if history_event and history_event.selection else []
    if picked_rows:
        picked_run = past_runs[picked_rows[0]]
        picked_run_id = picked_run["run_id"]
        if picked_run["status"] == "running":
            # finished_at이 없으면 elapsed_seconds가 now()로 계속 새로 계산해서, 브라우저를 닫거나
            # 서버가 죽어 끝맺음 처리(finish_run)가 못 된 실행은 화면에서 소요시간이 영영 흐르는
            # 것처럼 보인다. 실제로 지금 다른 탭/세션에서 진행 중일 수도 있으니 자동으로 정리하지
            # 않고, 사람이 확인 후 눌러야만 정리한다.
            st.warning(
                "이 실행은 아직 **running** 상태입니다. 다른 탭/세션에서 실제로 진행 중일 수도 있고, "
                "브라우저를 닫거나 서버가 중간에 죽어서 끝맺음 처리가 안 된 채로 멈춰 있을 수도 있습니다 "
                "(그래서 소요시간이 계속 흐르는 것처럼 보입니다). **정말로 멈췄다고 확신할 때만** 아래 "
                "버튼으로 정리하세요."
            )
            if st.button("🛑 멈춘 실행으로 표시", key=f"mark_stopped_{picked_run_id}"):
                runs_repo.finish_run(conn, picked_run_id, "stopped", {}, ["사용자가 수동으로 중단 처리함"])
                _auto_export_accepted(conn, configs, picked_run_id)  # 죽기 전까지 accepted된 것만이라도 CSV에 남긴다
                st.rerun()
        if picked_run_id != last_run_id:
            _render_run_results(conn, configs, picked_run_id)
