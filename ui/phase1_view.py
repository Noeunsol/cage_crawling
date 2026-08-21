"""① 1차 수집 화면 — 트렌드 수집 실행 + 최근 실행·사전 필터·수집원 비교.

src/phase1의 관찰·실행 창구다. 2차 화면(phase2_view)과 코드를 공유하지 않는다.
"""
from __future__ import annotations

import os
import streamlit as st
import time
import yaml
from pathlib import Path

from src.common.classify import load_llm_config
from ui.common import (
    STATUS_LABEL, collection_rows, dependency_status, fmt_dur, query, scalar,
)

def render_collection(tabs, *, db_path: str, trend_cfg: str, taxo_cfg: str,
                      settings_cfg: str, p2_config: str) -> None:
    """수집 실행 탭. 첫 수집의 진입점이라 DB가 아직 없어도 떠야 한다."""
    trend_collection_tab = tabs[0]
    with trend_collection_tab:
        st.subheader("1차 트렌드 수집")
        st.caption("수집 기간과 수집원을 정한 뒤, 미리보기 또는 실제 수집을 실행합니다.")
        # 수집원 목록·상한은 trend_collection.yaml이 정본이다. UI에 하드코딩하면 소스를 추가할 때마다
        # 화면과 설정이 어긋난다(실제로 일베·닥터나우가 설정에만 있고 UI엔 없었다).
        try:
            _trend_cfg = yaml.safe_load(Path(trend_cfg).read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            _trend_cfg = {}
        _max_lookback = int(_trend_cfg.get("collection", {}).get("max_lookback_days", 30))
        _sampling = _trend_cfg.get("sampling", {})
        _daily_quota = _sampling.get("daily_quota_by_source", {})
        _equal_quota = bool(_sampling.get("equal_quota_by_source", False))
        _labels = {"dcinside": "디시인사이드", "news_rss": "뉴스 RSS",
                   "ilbe": "일간베스트", "doctornow": "닥터나우 (의료 상담)"}
        _sources = [(name, cfg or {}) for name, cfg in (_trend_cfg.get("sources") or {}).items()]
    
        st.markdown("**수집 기간 · 수집원**")
        source_enabled, source_caps = {}, {}
        for _name, _scfg in _sources:
            source_enabled[_name] = st.checkbox(
                _labels.get(_name, _name), value=bool(_scfg.get("enabled", True)), key=f"src_{_name}")
        st.caption("FM코리아·네이트판은 robots.txt가 크롤러를 차단해 제외했습니다. 로톡은 JS 렌더가 필요해 보류 중입니다.")
        lookback_days = st.number_input(
            "최근 며칠", min_value=1, max_value=_max_lookback, value=1, step=1,
            help=f"오늘부터 거슬러 며칠분을 수집할지. 설정 파일 상한은 {_max_lookback}일입니다.")
        st.metric("수집 범위", f"최근 {int(lookback_days)}일")
        _cols = st.columns(min(len(_sources), 4) or 1)
        for _i, (_name, _scfg) in enumerate(_sources):
            source_caps[_name] = _cols[_i % len(_cols)].number_input(
                f"{_labels.get(_name, _name)} 1일 상한", min_value=0, max_value=1000,
                value=int(_daily_quota.get(_name, 30)), step=10, key=f"cap_{_name}",
                disabled=not source_enabled[_name],
                help="이 수집원에서 하루에 본문 수집할 최대 건수입니다. 0이면 수집하지 않습니다.")
        source_daily = sum(v for k, v in source_caps.items() if source_enabled.get(k))
        _detail = " · ".join(f"{_labels.get(k, k)} {v}" for k, v in source_caps.items() if source_enabled.get(k))
        st.caption(
            f"본문 후보 상한 {source_daily * int(lookback_days):,}건 (날짜당 {_detail}, 4시간대 균등 표본). "
            "실제 처리 건수는 기간 내 가용 후보에서 중복·제목 필터를 제외한 수입니다."
        )
        if _equal_quota:
            st.info("**균등 quota 모드** — 수집원별 성과 가중 재분배를 끄고 위 값을 그대로 씁니다. "
                    "수집원 비교 실험용이며, 끄려면 `sampling.equal_quota_by_source: false`로 두세요.")
        st.caption("해당 기간에 게시된 가용 후보를 수집하고, 제목 필터와 LLM을 거쳐 taxonomy를 매핑합니다.")
        st.caption("본문 키워드 룰로 재탈락시키지 않으며, 제목과 HTML 본문을 LLM이 최종 분류합니다.")
        reset = st.checkbox("기존 DB 비우고 새로 수집")
    
        try:
            settings_data = yaml.safe_load(Path(settings_cfg).read_text(encoding="utf-8"))
        except Exception:
            settings_data = {}
        with st.expander("LLM 분류 상태"):
            try:
                llm_cfg = settings_data.get("matching", {})
                llm_eff = load_llm_config(settings_data)   # configs/llm.yaml + settings.matching.llm 병합
                provider = llm_eff.get("provider", "anthropic")
                model = llm_eff.get("model", "")
                env_name = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
                module = "openai" if provider == "openai" else "anthropic"
                ready, ready_reason = dependency_status(
                    module, env_name, bool(llm_eff.get("enabled", False)))
                if ready:
                    st.success(f"LLM 로컬 준비 완료 · {ready_reason}")
                else:
                    st.error(f"LLM 준비 안 됨 · {ready_reason}")
                st.write(f"Provider: `{provider}` · Model: `{model}`")
                st.write("공통 classifier 1회 · 19개 Lv2 Definition/Description 전체 비교")
                st.caption(
                    f"Accepted: confidence ≥ {llm_cfg.get('accepted_confidence', 0.75)}, "
                    f"Taxonomy fit ≥ {llm_cfg.get('accepted_taxonomy_fit', 0.50)}, "
                    f"Concrete context ≥ {llm_cfg.get('accepted_concrete_context', 0.30)}, "
                    f"Korea relevance ≥ {llm_cfg.get('min_korea_relevance', 0.30)}"
                )
                if not os.getenv(env_name):
                    st.code(f'{env_name}="sk-..."', language="bash")
                    st.caption("프로젝트 루트의 .env에 추가한 뒤 '.env 다시 읽기'를 누르세요. 키 값은 화면과 DB에 저장하지 않습니다.")
            except Exception as exc:  # noqa: BLE001
                st.caption(f"분류 설정 확인 실패: {exc}")
    
        overrides = {
            "collection": {"lookback_days": int(lookback_days)},
            "sampling": {
                "daily_quota_by_source": {k: int(v) for k, v in source_caps.items()},
            },
            "enabled": dict(source_enabled),
        }
    
        b1, b2 = st.columns(2)
        if b1.button("미리보기"):
            with st.spinner("가용 규모 확인 중…"):
                try:
                    from src.phase1 import run as pipeline
                    rep = pipeline.run_trend(trend_config=trend_cfg, taxonomy_config=taxo_cfg,
                                             settings_config=settings_cfg, db_path=db_path,
                                             dry_run=True, overrides=overrides)
                    st.success(f"제목 단계 상세수집 후보 약 {rep.get('would_extract', 0):,}건")
                    st.caption("미리보기는 본문과 LLM을 호출하지 않으므로 최종 accepted 수는 실제 실행 후 확정됩니다.")
                    if rep.get("collection_targets"):
                        st.dataframe(collection_rows(rep["collection_targets"], preview=True), hide_index=True)
                    st.json(rep.get("by_source", {}))
                except Exception as exc:  # noqa: BLE001 (UI 표시)
                    st.error(f"실패: {exc}")
    
        if b2.button("1차 수집 시작", type="primary"):
            if not any(source_enabled.values()):
                st.warning("수집원을 하나 이상 선택하세요.")
            else:
                bar = st.progress(0.0, text="목록 수집 중…")
    
                def _prog(done, total):
                    bar.progress(done / total if total else 1.0,
                                 text=f"가용·필터 통과 후보 {done}/{total}건 처리 중…")
    
                try:
                    from src.phase1 import run as pipeline
                    _t0 = time.perf_counter()
                    rep = pipeline.run_trend(trend_config=trend_cfg, taxonomy_config=taxo_cfg,
                                             settings_config=settings_cfg, db_path=db_path, reset_db=reset,
                                             overrides=overrides, on_progress=_prog)
                    _elapsed = time.perf_counter() - _t0
                    bar.progress(1.0, text="완료")
                    st.cache_data.clear()
                    st.success(f"저장 {rep.get('stored_records', 0):,}건 · 소요 {fmt_dur(_elapsed)} · 최근 실행 탭에서 확인")
                    if rep.get("by_action"):
                        st.caption("최종 분류 결과 (accepted/discard)")
                        st.json(rep["by_action"])
                    if rep.get("collection_targets"):
                        st.caption(f"최근 {int(lookback_days)}일 수집 결과")
                        st.dataframe(collection_rows(rep["collection_targets"]), hide_index=True)
                    window_result = rep.get("collection_window", {})
                    if window_result:
                        st.info(
                            f"최근 {window_result.get('lookback_days', lookback_days)}일 · "
                            f"본문 표본 {window_result.get('selected', 0)} · "
                            f"accepted {window_result.get('accepted', 0)}"
                        )
                except Exception as exc:  # noqa: BLE001 (UI 표시)
                    st.error(f"실행 실패: {exc}")


def render_reports(tabs, *, db_path: str, trend_cfg: str, taxo_cfg: str,
                   settings_cfg: str, p2_config: str) -> None:
    """1차 결과 조회 탭. DB가 있어야 하므로 호출부가 존재 확인 뒤에 부른다."""
    _, recent_tab, prefilter_tab, source_compare_tab = tabs

    with recent_tab:
        st.subheader("최근 실행한 1차 수집")
        st.caption("선택한 DB에서 가장 마지막으로 완료한 1차 실행 1건만 표시합니다. 새로고침해도 유지됩니다.")
        recent_runs = query(db_path, """
            SELECT run_id, MAX(rowid) AS latest_rowid,
                   COUNT(*) AS candidates,
                   SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END) AS accepted,
                   SUM(CASE WHEN status='extraction_failed' THEN 1 ELSE 0 END) AS extraction_failed,
                   SUM(CASE WHEN status NOT IN ('accepted', 'discovered', 'extraction_failed') THEN 1 ELSE 0 END) AS excluded
            FROM url_candidates
            WHERE collection_phase=1 AND COALESCE(run_id, '') != ''
            GROUP BY run_id
            ORDER BY latest_rowid DESC
            LIMIT 1
        """)
        if recent_runs:
            recent_run = recent_runs[0]
            run_id = recent_run["run_id"]
            st.caption(f"실행 ID `{run_id}` · DB=`{db_path}`")
            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("처리 후보", f"{recent_run['candidates'] or 0:,}")
            mc2.metric("최종 저장", f"{recent_run['accepted'] or 0:,}")
            mc3.metric("본문 수집 실패", f"{recent_run['extraction_failed'] or 0:,}")
            mc4.metric("제외·중복", f"{recent_run['excluded'] or 0:,}")
            accepted_rows = query(db_path, """
                SELECT title,source,taxonomy_lv1,taxonomy_lv2,category,
                       risk_score,trend_score,confidence,source_url
                FROM content_records
                WHERE collection_phase=1 AND run_id=? AND action='accepted'
                ORDER BY rowid DESC LIMIT 500
            """, (run_id,))
            discarded_rows = query(db_path, """
                SELECT title,source,status,filter_reason,source_url
                FROM url_candidates
                WHERE collection_phase=1 AND run_id=?
                  AND status NOT IN ('accepted','discovered')
                ORDER BY rowid DESC LIMIT 500
            """, (run_id,))
            accepted_tab, discarded_tab = st.tabs([f"✅ accepted 콘텐츠 ({len(accepted_rows)})", f"제외·실패 후보 ({len(discarded_rows)})"])
            with accepted_tab:
                st.caption("본문을 수집하고 taxonomy 분류까지 통과해 저장된 콘텐츠입니다.")
                st.dataframe(accepted_rows, hide_index=True, width="stretch",
                             column_config={"source_url": st.column_config.LinkColumn("원문", display_text="열기")})
            with discarded_tab:
                st.caption("1차는 discard 본문을 저장하지 않습니다. 대신 제목·처리 상태·제외 사유를 남깁니다.")
                st.dataframe([
                    {"제목": row["title"] or "(제목 없음)", "수집원": row["source"] or "—",
                     "처리 결과": STATUS_LABEL.get(row["status"], row["status"]),
                     "사유": row["filter_reason"] or "—", "원문": row["source_url"]}
                    for row in discarded_rows
                ], hide_index=True, width="stretch",
                    column_config={"원문": st.column_config.LinkColumn("원문", display_text="열기")})
        else:
            st.info("실행 ID가 기록된 1차 수집 결과가 없습니다. 위에서 1차 수집을 한 번 실행해 보세요.")

    with prefilter_tab:
        st.subheader("1차 수집 · 원문을 열기 전 제외한 후보")
        st.caption("1차 수집에서 제목만 보고 제외한 기록입니다. 본문·댓글·RSS 요약은 이 단계에서 사용하거나 저장하지 않습니다.")
        pc = st.columns(3)
        pc[0].metric("크롤링 절약", scalar(
            db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status LIKE 'prefilter_%' AND COALESCE(collection_phase,1)=1"))
        pc[1].metric("Discarded", scalar(
            db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status='prefilter_discarded' AND COALESCE(collection_phase,1)=1"))
        pc[2].metric("Trend seed", scalar(
            db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status='prefilter_discarded' AND is_trend_seed=1 AND COALESCE(collection_phase,1)=1"))
    
        actions = ["전체", "discarded", "trend seed"]
        sources = ["전체"] + [row["source"] for row in query(
            db_path, "SELECT DISTINCT source FROM url_candidates WHERE source IS NOT NULL AND source!='' AND COALESCE(collection_phase,1)=1 ORDER BY source")]
        pf1, pf2 = st.columns(2)
        pf_action = pf1.selectbox("Pre-filter action", actions)
        pf_source = pf2.selectbox("Pre-filter 수집원", sources)
        where, params = ["status LIKE 'prefilter_%'", "COALESCE(collection_phase,1)=1"], []
        if pf_action == "discarded":
            where.append("filter_action='discard'")
        elif pf_action == "trend seed":
            where.append("is_trend_seed=1")
        if pf_source != "전체":
            where.append("source=?"); params.append(pf_source)
        pre_rows = query(
            db_path,
            f"""SELECT title,source,board_name,category_name,published_at_hint,
                       filter_action,filter_reason,source_url
                FROM url_candidates WHERE {' AND '.join(where)}
                ORDER BY rowid DESC LIMIT 500""",
            tuple(params),
        )
        st.dataframe(pre_rows, width="stretch", hide_index=True,
                     column_config={"source_url": st.column_config.LinkColumn("URL", display_text="열기")})

    with source_compare_tab:
        st.subheader("수집원 비교")
        st.caption("같은 수량 기준으로 수집원별 결과가 어떻게 갈리는지 봅니다. "
                   "총 수집 대비 accepted 비율(수율)과 어떤 taxonomy로 매핑되는지가 핵심 지표입니다.")
    
        since = st.selectbox("기간", ["전체", "최근 1일", "최근 7일", "최근 30일"], index=0,
                             help="content_records.collected_at 기준입니다.")
        _days = {"최근 1일": 1, "최근 7일": 7, "최근 30일": 30}.get(since)
        _where = f"WHERE date(collected_at) >= date('now', '-{_days} day')" if _days else ""
    
        rows = query(db_path, f"""
            SELECT COALESCE(source, site_type, 'unknown') AS 수집원,
                   COALESCE(NULLIF(board_name,''), NULLIF(category_name,''), '-') AS 카테고리,
                   COUNT(*) AS 총수집,
                   SUM(action='accepted') AS accepted,
                   SUM(action='discard') AS discard,
                   SUM(action NOT IN ('accepted','discard')) AS 기타
            FROM content_records {_where or 'WHERE 1=1'} AND COALESCE(is_supplementary,0)=0
            GROUP BY 수집원, 카테고리 ORDER BY 총수집 DESC
        """)
        if not rows:
            st.info("해당 기간에 수집된 콘텐츠가 없습니다.")
        else:
            for r in rows:
                r["수율"] = round(r["accepted"] / r["총수집"], 3) if r["총수집"] else 0.0
            st.dataframe(rows, hide_index=True, width="stretch",
                         column_config={"수율": st.column_config.ProgressColumn(
                             "accepted 수율", min_value=0.0, max_value=1.0, format="%.1f%%")})
    
            st.markdown("**수집원 × taxonomy 매핑 양상**")
            st.caption("같은 수량을 모아도 수집원마다 어떤 taxonomy가 나오는지가 다릅니다. accepted 기준입니다.")
            tax = query(db_path, f"""
                SELECT COALESCE(source, site_type, 'unknown') AS 수집원,
                       COALESCE(taxonomy_lv2,'-') AS taxonomy, COUNT(*) AS 건수
                FROM content_records {_where or 'WHERE 1=1'} AND action='accepted'
                  AND COALESCE(is_supplementary,0)=0
                GROUP BY 수집원, taxonomy ORDER BY 수집원, 건수 DESC
            """)
            if tax:
                st.markdown("**taxonomy별 수집원 accepted 수량**")
                st.caption("가로축은 Taxonomy lv2, 세로축은 accepted 콘텐츠 수량이며 색상은 수집원입니다.")
                st.bar_chart(tax, x="taxonomy", y="건수", color="수집원")
                pivot: dict = {}
                for t in tax:
                    pivot.setdefault(t["taxonomy"], {"taxonomy": t["taxonomy"]})[t["수집원"]] = t["건수"]
                st.dataframe(sorted(pivot.values(),
                                    key=lambda r: -sum(v for k, v in r.items() if k != "taxonomy")),
                             hide_index=True, width="stretch")
            else:
                st.info("accepted 콘텐츠가 아직 없습니다.")
    
            st.markdown("**후보 단계 탈락 사유**")
            st.caption("본문을 가져오기 전에 어디서 걸러졌는지. 수율이 낮은 수집원의 원인을 여기서 확인합니다.")
            st.dataframe(query(db_path, """
                SELECT COALESCE(source, source_type, 'unknown') AS 수집원,
                       COALESCE(NULLIF(status,''),'-') AS 후보상태, COUNT(*) AS 건수
                FROM url_candidates
                WHERE COALESCE(discovery_method,'') <> 'in_body_link'
                  AND COALESCE(is_supplementary,0)=0
                GROUP BY 수집원, 후보상태 ORDER BY 수집원, 건수 DESC
            """), hide_index=True, width="stretch")
