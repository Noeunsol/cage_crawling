"""② 2차 수집 화면 — taxonomy 목표 수집 실행 + 결과·재검수.

src/phase2의 관찰·실행 창구다. 1차 화면(phase1_view)과 코드를 공유하지 않는다.
"""
from __future__ import annotations

import json
import pandas as pd
import streamlit as st
import time

from collections import Counter

from src.common import paths
from src.phase2.config import load_phase2_config
from ui.common import (
    ACTION_LABEL, STATUS_LABEL, dependency_status, fmt_date, fmt_dur, has_column, query, run_label, scalar,
)

def render(tabs, *, db_path: str, trend_cfg: str, taxo_cfg: str,
           settings_cfg: str, p2_config: str) -> None:
    taxonomy_collection_tab, phase2_results_tab = tabs
    try:
        phase2_cfg = load_phase2_config(p2_config) or {}
    except OSError:
        phase2_cfg = {}
    with taxonomy_collection_tab:
        st.subheader("2차 Taxonomy 수집")
        st.caption("전체 taxonomy coverage를 확인한 뒤 필요한 LV2만 체크하세요. 선택한 taxonomy의 부족 type만 계획된 provider 순서로 수집합니다.")
        try:
            from src.phase2 import run as _taxonomy_pipeline
            taxonomy_plan_rows = _taxonomy_pipeline.preview_taxonomy_plan(p2_config, db_path, taxonomy_config=taxo_cfg)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Taxonomy 수집 계획을 읽지 못했습니다: {exc}")
            taxonomy_plan_rows = []
    
        if taxonomy_plan_rows:
            summary_slot = st.container()
            coverage_table = pd.DataFrame([{
                "선택": False,
                "Taxonomy lv1": row["lv1"],
                "Taxonomy lv2": row["lv2"],
                "현재": row["effective"],
                "목표": row["target"],
                "부족": row["shortfall"],
                "부족 Type": sum(type_row["shortfall"] > 0 for type_row in row["types"]),
                "수집 전략": " → ".join(row["provider_order"]),
                "상태": ("목표 도달" if row["shortfall"] <= 0 else "일부 부족" if row["effective"] > 0 else "수집 필요"),
            } for row in taxonomy_plan_rows])
            st.markdown("#### Taxonomy coverage")
            edited_coverage = st.data_editor(
                coverage_table, width="stretch", hide_index=True, key="taxonomy_first_coverage_table",
                disabled=[column for column in coverage_table.columns if column != "선택"],
                column_config={"선택": st.column_config.CheckboxColumn("선택", default=False)},
            )
            selected_taxonomies = edited_coverage.loc[edited_coverage["선택"], "Taxonomy lv2"].tolist()
            selected_shortfall = sum(
                row["shortfall"] for row in taxonomy_plan_rows if row["lv2"] in selected_taxonomies
            )
            with summary_slot:
                pc1, pc2, pc3, pc4, pc5 = st.columns(5)
                pc1.metric("전체 LV2", len(taxonomy_plan_rows))
                pc2.metric("목표 도달", sum(row["shortfall"] <= 0 for row in taxonomy_plan_rows))
                pc3.metric("수집 부족", sum(row["shortfall"] > 0 for row in taxonomy_plan_rows))
                pc4.metric("선택 실행 대상", len(selected_taxonomies))
                pc5.metric("총 추가 필요", f"{selected_shortfall:g}건")
            selected_plans = [row for row in taxonomy_plan_rows if row["lv2"] in selected_taxonomies]
            if selected_plans:
                if "1_A_Toxic_Language" in selected_taxonomies:
                    st.info(
                        "**Toxic Language 뉴스 사건형 수집** · 최근 365일의 Tavily 전체 뉴스 + 핵심 언론사 site: 검색 · "
                        "본문에서 게시일·Toxic 근거·사건·200자 기준을 검증해 저장합니다."
                    )
                st.markdown("#### 선택 Taxonomy 상세 수집 계획")
                plan_table = [
                    {"Taxonomy lv2": row["lv2"], "Type": type_row["type"],
                     "현재": type_row["effective"], "목표": type_row["target"],
                     "추가 필요": type_row["shortfall"], "수집 경로": " → ".join(type_row["provider_order"])}
                    for row in selected_plans for type_row in row["types"]
                ]
                st.dataframe(plan_table, width="stretch", hide_index=True)
                tavily_ready, _ = dependency_status("tavily", "TAVILY_API_KEY")
                serpapi_ready, _ = dependency_status("serpapi", "SERPAPI_KEY")
                st.caption(
                    f"Provider 준비 상태 · Tavily: {'준비' if tavily_ready else '키 확인 필요'} · "
                    f"SerpAPI: {'준비' if serpapi_ready else '키 확인 필요'}"
                )
            tax_cols = st.columns(2)
            taxonomy_write_db = tax_cols[0].text_input("Small Run 저장 DB", paths.PHASE2_DB_DEFAULT, key="taxonomy_first_db")
            taxonomy_fetch_cap = tax_cols[1].number_input("type당 본문 fetch 상한", 1, 30, 6, key="taxonomy_first_fetch")

            # ── 이번 실행 수집량 ──
            # 크레딧은 검색 호출에서만 나간다. 본문 fetch는 HTTP라 무료고 시간만 든다.
            # 그래서 "아껴 쓰기"의 손잡이는 검색어 수 하나뿐이다.
            st.markdown("#### 이번 실행 수집량")
            _plan_default = int((phase2_cfg.get("query_planner", {}) or {}).get("max_queries_per_lv2", 12))
            _fetch_default = int((phase2_cfg.get("limits", {}) or {}).get("max_total_fetch", 800))
            run_cols = st.columns(2)
            run_max_queries = run_cols[0].slider(
                "💳 카테고리당 검색어 수 (크레딧)", 1, max(_plan_default, 68),
                min(15, _plan_default), key="taxonomy_max_queries",
                help="검색어 1개 = 검색 API 호출 1회 = 크레딧 1. 이번 실행 비용은 사실상 이 값이 정합니다. "
                     "낮추면 그만큼 적게 삽니다.")
            run_max_fetch = run_cols[1].slider(
                "본문 수집 상한 (무료·시간만 소요)", 10, max(_fetch_default, 800),
                min(100, _fetch_default), step=10, key="taxonomy_max_fetch",
                help="검색으로 찾은 후보 중 실제로 본문을 가져올 최대 건수입니다. "
                     "HTTP만 쓰므로 크레딧과 무관합니다.")
            _est_calls = run_max_queries * max(len(selected_taxonomies), 1)
            if selected_taxonomies:
                st.caption(
                    f"예상 검색 호출 **최대 {_est_calls}회** "
                    f"(검색어 {run_max_queries} × 카테고리 {len(selected_taxonomies)}개) · "
                    f"본문 수집 최대 {run_max_fetch}건. "
                    "실제 호출은 검색 계획이 상한보다 적으면 더 적습니다."
                )
            query_plan_mode_label = st.radio(
                "검색어 생성 방식",
                ["OpenAI로 생성", "YAML 쿼리 랜덤 사용"],
                horizontal=True,
                key="taxonomy_query_plan_mode",
                help="OpenAI 생성은 taxonomy와 최근 seed를 반영합니다. YAML 쿼리는 targeted_collection.yaml의 고정 검색어를 섞어서 사용합니다.",
            )
            query_plan_mode = "openai" if query_plan_mode_label == "OpenAI로 생성" else "yaml_random"

            # YAML 모드는 뽑기(shuffle)로 검색어를 고른다. 실행 전에 무엇이 나갈지 그대로 보여주고,
            # seed를 실행에 함께 넘겨 미리보기와 실제 검색어를 일치시킨다.
            yaml_seed = st.session_state.setdefault("taxonomy_yaml_seed", 0)
            if query_plan_mode == "yaml_random":
                seed_col, _ = st.columns([1, 3])
                if seed_col.button("🎲 검색어 다시 뽑기", key="taxonomy_reshuffle"):
                    yaml_seed = st.session_state["taxonomy_yaml_seed"] = yaml_seed + 1
                if selected_taxonomies:
                    try:
                        _preview = _taxonomy_pipeline.preview_yaml_queries(
                            p2_config, selected_taxonomies,
                            overrides={"query_planner": {
                                "enabled": False, "shuffle_fallback_plans": True,
                                "shuffle_seed": yaml_seed,
                                "max_queries_per_lv2": int(run_max_queries)}},
                        )
                    except Exception as exc:  # noqa: BLE001
                        _preview = {}
                        st.error(f"YAML 검색어를 읽지 못했습니다: {exc}")
                    _q = [dict(item, lv2=lv2) for lv2, v in _preview.items() for item in v["queries"]]
                    _boards = [dict(b, lv2=lv2) for lv2, v in _preview.items() for b in v["boards"]]
                    _idle = [dict(i, lv2=lv2) for lv2, v in _preview.items()
                             for i in v["idle"] if not v["two_hop"]]
                    _two_hop = [lv2 for lv2, v in _preview.items() if v["two_hop"]]
                    if _q or _boards:
                        _by_provider = Counter(item["provider"] for item in _q)
                        _mix = " · ".join(f"{name} {n}건" for name, n in _by_provider.most_common()) or "검색어 없음"
                        st.caption(f"이번 실행 검색어 **{len(_q)}개** — {_mix} (뽑기 #{yaml_seed}). "
                                   "아래 그대로 검색합니다.")
                        # provider별로 나눠 보여준다. 어느 채널로 얼마나 나가는지가 이 화면의 요점이다.
                        for _prov in _by_provider:
                            st.markdown(f"**{_prov}** · {_by_provider[_prov]}건")
                            st.dataframe(
                                [{"카테고리": item["lv2"], "검색어": item["query"],
                                  "Type": item["target_type"] or "—",
                                  "실제 검색식": item["final_query"],
                                  "검색 대상": ", ".join(item["targets"]) if item["targets"] else "웹 전체",
                                  "source": item["source_id"]}
                                 for item in _q if item["provider"] == _prov],
                                width="stretch", hide_index=True)
                        if _boards:
                            st.markdown("**게시판 목록** · 검색어를 쓰지 않고 최신 글을 그대로 가져옵니다")
                            st.dataframe(
                                [{"카테고리": b["lv2"], "source": b["source_id"],
                                  "대상": ", ".join(b["targets"]) if b["targets"] else "—"}
                                 for b in _boards], width="stretch", hide_index=True)
                        _exec = [dict(e, lv2=lv2) for lv2, v in _preview.items() for e in v["execution"]]
                        if _exec:
                            with st.expander("⚙️ 검색 API에 실제로 나가는 요청 (provider별 실행 방식)"):
                                st.caption("아래 값은 수집 실행과 **같은 코드**로 만든 것입니다. "
                                           "크레딧은 검색 호출 1회당 1이며, SerpAPI 다중 도메인은 "
                                           "`site:` OR 한 줄로 묶어 1회로 처리합니다.")
                                for _e in _exec:
                                    st.markdown(f"**{_e['provider']}** · `{_e['lv2']}` · `{_e['source_id']}`")
                                    st.dataframe([{"항목": k, "값": str(v)} for k, v in _e["options"].items()],
                                                 width="stretch", hide_index=True)
                        if _two_hop:
                            st.info(
                                "**" + ", ".join(_two_hop) + "** 은(는) 2단계 수집입니다 — "
                                "위 검색어는 1단계(공식기관에서 사건 파악)용이고, "
                                "찾은 사건으로 2단계 검색어를 만들어 언론 기사를 수집합니다."
                            )
                        if _idle:
                            st.warning(
                                "이번 실행에 **검색어가 배정되지 않은 수집원**: "
                                + ", ".join(f"{i['lv2']} · {i['source_id']}({i['provider']})" for i in _idle)
                                + " — 이 카테고리의 `planner.source_mix` 비율을 확인하세요."
                            )
                    else:
                        st.warning("이 카테고리에는 YAML 검색어가 없습니다. "
                                   "`collection_intents_by_lv2`의 `queries_by_type`을 확인하세요.")
                else:
                    st.caption("보강할 카테고리를 고르면 실제로 나갈 검색어를 여기에 보여줍니다.")
            taxonomy_tuning = {}
            if selected_taxonomies:
                with st.expander("🎚️ 카테고리별 수집 기간 (기본 1년)"):
                    st.caption("**기간** 밖 콘텐츠는 검색에서 빼고 저장하지 않습니다. "
                               "수집 목표는 부족분 순위를 매길 때만 쓰고 수집을 멈추지 않습니다 — "
                               "이번 실행의 양은 아래 **fetch 상한**과 카테고리당 검색어 수가 정합니다.")
                    for _lv2 in selected_taxonomies:
                        taxonomy_tuning[_lv2] = {
                            "recency_days": st.number_input(
                                f"{_lv2} · 수집 기간(일)", 7, 3650, 365, step=30,
                                key=f"taxonomy_recency_{_lv2}"),
                        }
            st.caption(
                "수집한 본문은 검수 없이 목표 taxonomy로 바로 통합합니다(LLM 본문 재분류는 수집 후 재검수에서만). "
                "검색 계획 방식 카테고리는 type·provider로 쪼개지 않고 카테고리당 1회 실행되며, "
                "저장 여부는 규칙 기반 acceptance gate(한국 직접 관련 + 수집 기간 + 본문 품질)가 판정합니다. "
                "목표 LV2 근거는 저장을 막지 않고 근거만 기록합니다(2차 수집 결과 탭에서 확인)."
            )
            if st.button("선택한 Taxonomy 부족분 수집 실행", type="primary", key="taxonomy_first_run",
                         disabled=not selected_taxonomies):
                _status = st.status("2차 수집 준비 중…", expanded=True)
                _step_bar = _status.progress(0.0, text="실행 계획 계산 중")
                _fetch_bar = _status.progress(0.0, text="본문 수집 대기")
                _t0 = time.perf_counter()
    
                def _taxonomy_progress(step, total_steps, label, done, total):
                    _status.update(label=f"[{step}/{total_steps}] {label}")
                    _step_bar.progress(min(step / total_steps, 1.0) if total_steps else 0.0,
                                       text=f"실행 단위 {step}/{total_steps} (목표를 채우면 더 일찍 끝납니다)")
                    _fetch_bar.progress(min(done / total, 1.0) if total else 0.0,
                                        text=f"본문 수집 {done}/{total}" if total else "검색 중…")
    
                try:
                    report = _taxonomy_pipeline.run_taxonomy_plan(
                        selected_taxonomies, p2_config, taxonomy_write_db, taxonomy_config=taxo_cfg,
                        settings_config=settings_cfg, max_fetch_per_type=int(taxonomy_fetch_cap),
                        max_queries_per_lv2=int(run_max_queries),
                        max_total_fetch=int(run_max_fetch),
                        shuffle_seed=yaml_seed,
                        reference_db=db_path,
                        strategy_by_lv2=taxonomy_tuning,
                        query_plan_mode=query_plan_mode,
                        on_progress=_taxonomy_progress,
                    )
                    _step_bar.progress(1.0, text="완료")
                    _fetch_bar.progress(1.0, text="완료")
                    _status.update(label=f"완료 · {fmt_dur(time.perf_counter() - _t0)}",
                                   state="complete", expanded=False)
                    st.cache_data.clear()
                    st.session_state["_pending_result_db"] = taxonomy_write_db
                    st.session_state["_phase2_result_db"] = taxonomy_write_db
                    st.session_state["_phase2_recent_run_ids"] = [
                        run["run_id"] for run in report["runs"] if run.get("run_id")
                    ]
                    st.session_state["_phase2_recent_taxonomy_run_id"] = report.get("taxonomy_run_id", "")
                    _runs = report["runs"]
                    _new = sum(r.get("run_stored", 0) for r in _runs)
                    st.success(f"Taxonomy 계획 실행 완료 · 실행 {len(_runs)}회 · **이번 실행 저장 {_new}건**")
                    st.dataframe([
                        {"Taxonomy": run["lv2"], "Type": run["type"], "Provider": run["provider"],
                         "검색 후보": run.get("run_candidates", 0),
                         "이번 실행 저장": run.get("run_stored", 0),
                         "DB 누계": run.get("stored_records", 0),
                         "run_id": run.get("run_id", "")}
                        for run in _runs
                    ], width="stretch", hide_index=True)
                    # 저장 0건은 대개 실패가 아니라 '이미 목표만큼 모아둬서 검색을 안 한 것'이다.
                    # 이유를 안 보여주면 화면에는 "완료"만 남아 오류로 읽힌다.
                    for run in _runs:
                        for lv2, info in (run.get("skipped") or {}).items():
                            (st.warning if info.get("reason") == "target_already_met" else st.error)(
                                f"**{lv2}** — {info.get('detail', '')}")
                    if _new == 0 and not any(r.get("skipped") for r in _runs):
                        st.warning("이번 실행에서 저장된 콘텐츠가 없습니다. 아래 **2차 수집 결과** 탭에서 "
                                   "후보가 어느 단계에서 빠졌는지 확인하세요.")
                    st.info("상단 **2차 수집 결과** 탭에서 provider별 후보·본문 수집·OpenAI 검수 결과를 실행 이력별로 확인하세요.")
                except Exception as exc:  # noqa: BLE001
                    _status.update(label="실패", state="error", expanded=True)
                    st.error(f"Taxonomy 계획 수집 실패: {exc}")
        else:
            st.info("taxonomy 계획을 표시할 수 없습니다. taxonomy·targeted_collection 설정을 확인하세요.")
    
    with phase2_results_tab:
        st.subheader("2차 Taxonomy 수집 결과")
        phase2_result_db = st.session_state.get("_phase2_result_db", db_path)
        st.caption(
            "Taxonomy 계획에서 Tavily·SerpAPI를 순차 실행한 결과입니다. "
            "검색 후보, 본문 수집, OpenAI 검수와 저장된 본문을 실행 이력별로 확인합니다."
        )
        st.caption(f"조회 DB: `{phase2_result_db}`")
        has_taxonomy_run_id = has_column(phase2_result_db, "url_candidates", "taxonomy_run_id")
        if has_taxonomy_run_id:
            taxonomy_runs = query(phase2_result_db, """
                SELECT taxonomy_run_id, MAX(rowid) AS latest_row,
                       GROUP_CONCAT(DISTINCT discovery_provider) AS providers,
                       GROUP_CONCAT(DISTINCT taxonomy_lv2_candidate) AS taxonomies,
                       GROUP_CONCAT(DISTINCT NULLIF(subtype_candidate,'')) AS types,
                       COUNT(DISTINCT run_id) AS provider_runs, COUNT(*) AS discovered,
                       SUM(CASE WHEN status='extraction_failed' THEN 1 ELSE 0 END) AS extract_failed,
                       SUM(CASE WHEN status IN ('candidate','accepted') THEN 1 ELSE 0 END) AS stored
                FROM url_candidates
                WHERE collection_phase=2 AND taxonomy_run_id IS NOT NULL AND taxonomy_run_id!=''
                GROUP BY taxonomy_run_id ORDER BY latest_row DESC
            """)
        else:
            taxonomy_runs = []
        result_runs = query(phase2_result_db, """
            SELECT run_id,MAX(rowid) AS latest_row,COUNT(*) AS discovered,
                   COALESCE(MAX(NULLIF(discovery_provider,'')),
                       CASE WHEN run_id LIKE 'serpapi_%' THEN 'serpapi'
                            WHEN run_id LIKE 'tavily_%' THEN 'tavily' ELSE 'unknown' END) AS provider,
                   GROUP_CONCAT(DISTINCT taxonomy_lv2_candidate) AS taxonomies,
                   GROUP_CONCAT(DISTINCT NULLIF(subtype_candidate,'')) AS types,
                   SUM(CASE WHEN status='rerank_skipped' THEN 1 ELSE 0 END) AS skipped,
                   SUM(CASE WHEN status='extraction_failed' THEN 1 ELSE 0 END) AS extract_failed,
                   SUM(CASE WHEN status='candidate' THEN 1 ELSE 0 END) AS unverified,
                   SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END) AS accepted,
                   SUM(CASE WHEN status='discard' THEN 1 ELSE 0 END) AS discarded
            FROM url_candidates WHERE collection_phase=2 AND run_id IS NOT NULL AND run_id!=''
            GROUP BY run_id ORDER BY latest_row DESC
        """)
        if not result_runs:
            st.info("현재 조회 DB에는 2차 Taxonomy 수집 이력이 없습니다.")
        else:
            if taxonomy_runs:
                batch_by_id = {row["taxonomy_run_id"]: row for row in taxonomy_runs}
                selected_batch_id = st.selectbox(
                    "실행 선택",
                    list(batch_by_id),
                    key="phase2_results_taxonomy_batch",
                    format_func=lambda batch_id: (
                        f"{run_label(batch_id).split(' · ')[0]} · "
                        f"{batch_by_id[batch_id]['taxonomies'] or 'taxonomy 미확인'} · "
                        f"{batch_by_id[batch_id]['providers'] or '수집원 미확인'}"
                    ),
                )
                batch = batch_by_id[selected_batch_id]
                active_run_ids = [row["run_id"] for row in query(phase2_result_db, """
                    SELECT DISTINCT run_id FROM url_candidates
                    WHERE collection_phase=2 AND taxonomy_run_id=? AND run_id IS NOT NULL
                    ORDER BY rowid
                """, (selected_batch_id,))]
                st.caption(
                    f"선택한 실행은 `{batch['taxonomies'] or '—'}`를 대상으로 "
                    f"{batch['providers'] or '—'}를 순차 실행한 결과입니다. "
                    "아래 숫자와 콘텐츠는 해당 실행 전체를 합산합니다."
                )
            else:
                st.info("이전 형식의 실행 이력입니다. provider/type 단위로 결과를 표시합니다.")
                legacy_run_id = st.selectbox(
                    "실행 선택", [row["run_id"] for row in result_runs], key="phase2_results_legacy_run",
                    format_func=run_label,
                )
                active_run_ids = [legacy_run_id]
    
            run_placeholders = ",".join("?" for _ in active_run_ids)
            run_clause = f"run_id IN ({run_placeholders})"
            result_summary = query(phase2_result_db, f"""
                SELECT COUNT(*) AS discovered,
                       SUM(CASE WHEN status='rerank_skipped' THEN 1 ELSE 0 END) AS skipped,
                       SUM(CASE WHEN status='extraction_failed' THEN 1 ELSE 0 END) AS extract_failed,
                       SUM(CASE WHEN status='candidate' THEN 1 ELSE 0 END) AS unverified,
                       SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END) AS accepted,
                       SUM(CASE WHEN status IN ('discard','duplicate','quality_failed') THEN 1 ELSE 0 END) AS excluded
                FROM url_candidates WHERE collection_phase=2 AND {run_clause}
            """, tuple(active_run_ids))[0]
            result_metrics = st.columns(4)
            for col, label, value in zip(
                result_metrics, ["검색 후보", "본문 수집 실패", "최종 accepted", "제외·중복"],
                [result_summary["discovered"], result_summary["extract_failed"],
                 result_summary["accepted"], (result_summary["skipped"] or 0) + (result_summary["excluded"] or 0)],
            ):
                col.metric(label, value or 0)
            with st.expander("세부 실행 이력 보기", expanded=False):
                st.caption("한 taxonomy 실행 안에서 type별로 Tavily·SerpAPI가 처리한 세부 내역입니다. 문제가 있을 때만 확인하세요.")
                st.dataframe([
                    {
                        "실행 시각": run_label(row["run_id"]).split(" · ")[0],
                        "탐색 방식": row["provider"],
                        "대상 Taxonomy": row["taxonomies"] or "—",
                        "Targeted Type": row["types"] or "—",
                        "검색 후보": row["discovered"],
                        "본문 수집 실패": row["extract_failed"] or 0,
                        "accepted": row["accepted"] or 0,
                    }
                    for row in result_runs if row["run_id"] in active_run_ids
                ], width="stretch", hide_index=True)
            st.caption("‘검색 관련성’은 Tavily·SerpAPI의 제목·snippet 기반 후보 점수입니다. 본문을 읽은 뒤 산정하는 taxonomy fit(최종 적합도)과는 다르며, 낮은 후보는 본문 수집 전에 제외됩니다.")
            result_targets = [row["taxonomy_lv2_candidate"] for row in query(phase2_result_db, """
                SELECT DISTINCT taxonomy_lv2_candidate FROM url_candidates
                WHERE collection_phase=2 AND """ + run_clause + """ AND taxonomy_lv2_candidate IS NOT NULL
                ORDER BY taxonomy_lv2_candidate
            """, tuple(active_run_ids))]
            result_target = st.selectbox("대상 taxonomy", ["전체"] + result_targets, key="phase2_results_target")
            result_target_clause = "" if result_target == "전체" else " AND taxonomy_lv2_candidate=?"
            result_target_params = tuple(active_run_ids) if result_target == "전체" else (*active_run_ids, result_target)
    
            unverified_count = scalar(phase2_result_db, f"""
                SELECT COUNT(*) AS n FROM content_records
                WHERE {run_clause} AND collection_phase=2
                  AND (classification_source LIKE '%_unverified' OR classification_source LIKE '%_targeted')
                  {result_target_clause}
            """, result_target_params)
            if unverified_count and len(active_run_ids) == 1:
                with st.expander("고급 · 저장 콘텐츠 OpenAI 재검수", expanded=False):
                    st.markdown("#### 저장 콘텐츠 OpenAI 재검수")
                    st.caption("검색·본문 수집은 다시 하지 않고, 저장된 정제 본문만 OpenAI에 보냅니다(원문 raw는 전송하지 않음. PII 마스킹은 현재 미적용).")
                    vc1, vc2 = st.columns([1, 2])
                    verify_limit = vc1.number_input("검수할 후보 수", 1, int(unverified_count), int(unverified_count),
                                                    key="phase2_results_verify_limit")
                    if vc2.button("OpenAI 재검수 실행", key="phase2_results_verify", type="primary"):
                        try:
                            from src.phase2 import run as _p2_verify
                            verify_report = _p2_verify.verify_unverified_candidates(
                                active_run_ids[0], phase2_result_db, p2_config, taxonomy_config=taxo_cfg,
                                settings_config=settings_cfg,
                                target_lv2=None if result_target == "전체" else result_target,
                                # 재검수 임계는 targeted_collection.yaml의 acceptance/adjudication을 그대로 쓴다.
                                # (예전엔 삭제된 수동 실행 UI의 슬라이더 값을 넘겼다.)
                                limit=int(verify_limit), overrides=None,
                            )
                            st.cache_data.clear()
                            if verify_report.get("error"):
                                st.error(f"검수를 시작하지 못했습니다: {verify_report['error']}")
                            else:
                                st.success(f"OpenAI 검수 {verify_report['verified']}건 · 채택 {verify_report['accepted']}건 · 제외 {verify_report['discarded']}건")
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"기존 후보 OpenAI 검수 실패: {exc}")
            elif unverified_count:
                st.caption("미검수 후보 OpenAI 검수는 아래 세부 실행 이력에서 provider/type 실행 하나를 선택해 진행할 수 있습니다.")
    
            stored_result_tab, candidate_result_tab = st.tabs(["✅ 최종 accepted 콘텐츠", "전체 처리 결과"])
            with stored_result_tab:
                st.caption("이 실행에서 최종 accepted된 콘텐츠입니다. 행을 선택하면 아래에서 taxonomy 판정 근거와 정제 본문을 확인할 수 있습니다.")
                stored_rows = query(phase2_result_db, f"""
                    SELECT content_id,title,published_at,site_name,domain,discovery_provider,taxonomy_lv2_candidate AS target_taxonomy,
                           taxonomy_lv2 AS predicted_taxonomy,COALESCE(category, subtype) AS content_type,
                           action,source_url
                    FROM content_records WHERE {run_clause}{result_target_clause} AND action='accepted'
                    ORDER BY rowid DESC LIMIT 500
                """, result_target_params)
                st.caption(f"최종 accepted {len(stored_rows)}건")
                stored_table = [{
                    "제목": row["title"] or "(제목 없음)",
                    "작성일": row["published_at"] or "미제공",
                    "원문 사이트": row["site_name"] or row["domain"] or "—",
                    "Targeted Taxonomy": row["target_taxonomy"] or "—",
                    "Taxonomy lv2 (LLM)": row["predicted_taxonomy"] or "—",
                    "Type (LLM)": row["content_type"] or "—",
                    "처리 상태": ACTION_LABEL.get(row["action"], row["action"]),
                    "URL": row["source_url"],
                } for row in stored_rows]
                stored_event = st.dataframe(
                    stored_table, width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row",
                    key="phase2_stored_result_table",
                    column_config={"URL": st.column_config.LinkColumn("URL", display_text="열기")},
                )
                stored_selected = stored_event.selection.rows
                selected_stored_index = stored_selected[0] if stored_selected else None
                if stored_rows and selected_stored_index is not None and selected_stored_index < len(stored_rows):
                    selected_stored_id = stored_rows[selected_stored_index]["content_id"]
                    has_core_text = has_column(phase2_result_db, "content_records", "core_text")
                    qa_fields = "question_body,answer_body," if has_core_text else "'' AS question_body,'' AS answer_body,"
                    display_field = "COALESCE(NULLIF(core_text,''), body_text) AS display_text" if has_core_text else "body_text AS display_text"
                    stored_detail = query(phase2_result_db, f"""
                        SELECT title,source_url,published_at,source,extractor,{qa_fields}
                               {display_field},filter_reason,
                               taxonomy_lv2_candidate,taxonomy_lv1,taxonomy_lv2,COALESCE(category, subtype) AS content_type,
                               action,classification_source,classification_reason,
                               taxonomy_fit_score,harmfulness_score,korean_language_ratio,korea_relevance_score,
                               korea_context_evidence,concrete_context_score,evidence_spans
                        FROM content_records WHERE content_id=?
                    """, (selected_stored_id,))[0]
                    st.markdown(f"### {stored_detail['title'] or '(제목 없음)'}")
                    st.caption(
                        f"Targeted: `{stored_detail['taxonomy_lv2_candidate'] or '—'}` · "
                        f"LLM: `{stored_detail['taxonomy_lv1'] or '—'} → {stored_detail['taxonomy_lv2'] or '—'} → {stored_detail['content_type'] or '—'}` · "
                        f"{ACTION_LABEL.get(stored_detail['action'], stored_detail['action'])}"
                    )
                    if stored_detail["classification_reason"]:
                        st.caption(f"Taxonomy 판정 근거 · {stored_detail['classification_reason']}")
                    detail_scores = [
                        ("Taxonomy fit", "taxonomy와의 적합도", stored_detail["taxonomy_fit_score"]),
                        ("Harmfulness", "유해성 강도", stored_detail["harmfulness_score"]),
                        ("Korea context", "한국 관련 맥락 — 본문 전체 기준", stored_detail["korea_relevance_score"]),
                        ("Concrete context", "실제 사례·행위 등 구체적 맥락", stored_detail["concrete_context_score"]),
                    ]
                    for column, (label, description, score) in zip(st.columns(4), detail_scores):
                        column.markdown(
                            f"**{label}**  \n<span style='color: #6b7280'>: {description}</span>\n\n## {score}",
                            unsafe_allow_html=True,
                        )
                    korea_evidence = json.loads(stored_detail["korea_context_evidence"] or "[]")
                    st.caption(
                        f"본문 한글 비율: {stored_detail['korean_language_ratio']} · "
                        f"한국 맥락 근거: {' · '.join(korea_evidence) if korea_evidence else '감지되지 않음'}"
                    )
                    with st.expander("핵심 본문", expanded=True):
                        st.write(stored_detail["display_text"] or "(본문 없음)")
                    if stored_detail["question_body"] or stored_detail["answer_body"]:
                        with st.expander("Q&A 구조 보기", expanded=False):
                            if stored_detail["question_body"]:
                                st.markdown("**질문**")
                                st.write(stored_detail["question_body"])
                            if stored_detail["answer_body"]:
                                st.markdown("**답변**")
                                st.write(stored_detail["answer_body"])
                    st.caption(
                        f"{stored_detail['source'] or '—'} · {stored_detail['extractor'] or '—'} · "
                        f"{stored_detail['published_at'] or '날짜 없음'} · [원문 열기]({stored_detail['source_url']})"
                    )
                    if stored_detail["filter_reason"]:
                        st.caption(f"판정 사유 · {stored_detail['filter_reason']}")
    
            with candidate_result_tab:
                result_status = st.selectbox(
                    "결과 상태", ["전체", "미검수 후보", "OpenAI 검수 후보", "본문 수집 실패", "검색 단계 제외", "본문 확인 후 제외"],
                    key="phase2_results_status",
                )
                status_map = {
                    "미검수 후보": "candidate", "OpenAI 검수 후보": "accepted",
                    "본문 수집 실패": "extraction_failed", "검색 단계 제외": "rerank_skipped", "본문 확인 후 제외": "discard",
                }
                result_status_clause = "" if result_status == "전체" else " AND status=?"
                result_params = result_target_params if result_status == "전체" else (*result_target_params, status_map[result_status])
                result_rows = query(phase2_result_db, f"""
                    SELECT title,published_at_hint,taxonomy_lv2_candidate,subtype_candidate,site_name,domain,discovery_provider,status,
                           ROUND(discovery_relevance_score,2) AS discovery_score,
                           ROUND(korea_relevance_score,2) AS korea_score,filter_reason,source_url
                    FROM url_candidates WHERE {run_clause}{result_target_clause}{result_status_clause} ORDER BY rowid DESC LIMIT 500
                """, result_params)
                st.dataframe([
                    {"제목": row["title"] or "(제목 없음)", "원문 사이트": row["site_name"] or row["domain"] or "—",
                     "검색 제공 발행일": fmt_date(row["published_at_hint"]),
                     "대상 taxonomy": row["taxonomy_lv2_candidate"], "Targeted Type": row["subtype_candidate"] or "—",
                     "처리 결과": STATUS_LABEL.get(row["status"], row["status"]),
                     "검색 관련성": row["discovery_score"], "한국성(본문 기준)": row["korea_score"],
                     "이유": row["filter_reason"] or "—",
                     "원문": row["source_url"]}
                    for row in result_rows
                ], width="stretch", hide_index=True,
                    column_config={"원문": st.column_config.LinkColumn("원문", display_text="열기")})
