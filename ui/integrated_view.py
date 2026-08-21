"""통합 보기 — 1차·2차 결과를 같은 SQLite에서 합쳐 본다.

수집 구현은 phase별로 완전히 다르지만 저장 스키마가 하나라 여기서만 합쳐 볼 수 있다.
여기서는 실행하지 않고 읽기만 한다.
"""
from __future__ import annotations

import json
import pandas as pd
import streamlit as st
import yaml
from pathlib import Path

from src.common import paths
from ui.common import (
    ACTION_LABEL, METHOD_LABEL, STATUS_LABEL, has_column, query, scalar,
)

def render(tabs, *, db_path: str, trend_cfg: str, taxo_cfg: str,
           settings_cfg: str, p2_config: str) -> None:
    taxonomy_tab, overview, content_tab, candidates_tab, llm_tab, failures_tab = tabs
    total_candidates = scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates")
    total_content = scalar(db_path, "SELECT COUNT(*) AS n FROM content_records")
    with taxonomy_tab:
        st.subheader("통합 Taxonomy 커버리지")
        st.caption(
            "`content.db`(1차 운영 DB)와 `phase2_pilot.db`(2차 보강 DB)의 accepted를 합산합니다. "
            "같은 URL은 운영 DB를 우선해 한 번만 계산합니다."
        )
        taxonomy_rows_by_key = {}
        seen_urls = set()
        coverage_db_paths = (paths.DB_DEFAULT, paths.PHASE2_DB_DEFAULT)
        for coverage_db_path in coverage_db_paths:
            if not Path(coverage_db_path).is_file():
                continue
            coverage_records = query(coverage_db_path, """
                SELECT taxonomy_lv1, taxonomy_lv2, source_url, canonical_url, content_id,
                       COALESCE(collection_phase, 1) AS collection_phase,
                       COALESCE(llm_total_tokens, 0) AS tokens,
                       COALESCE(llm_estimated_cost_usd, 0) AS cost_usd
                FROM content_records
                WHERE action='accepted' AND COALESCE(is_supplementary,0)=0
                  AND taxonomy_lv2 IS NOT NULL AND taxonomy_lv2!=''
            """)
            for record in coverage_records:
                dedup_key = record["canonical_url"] or record["source_url"] or record["content_id"]
                if dedup_key in seen_urls:
                    continue
                seen_urls.add(dedup_key)
                key = (record["taxonomy_lv1"] or "", record["taxonomy_lv2"])
                row = taxonomy_rows_by_key.setdefault(key, {
                    "taxonomy_lv1": key[0], "taxonomy_lv2": key[1],
                    "total_accepted": 0, "phase1": 0, "phase2": 0,
                    "tokens": 0, "cost_usd": 0.0,
                })
                row["total_accepted"] += 1
                row["phase2" if int(record["collection_phase"] or 1) == 2 else "phase1"] += 1
                row["tokens"] += int(record["tokens"] or 0)
                row["cost_usd"] += float(record["cost_usd"] or 0)
        taxonomy_rows = list(taxonomy_rows_by_key.values())
        try:
            taxonomy_data = yaml.safe_load(Path(taxo_cfg).read_text(encoding="utf-8")) or {}
            phase2_data = yaml.safe_load(Path(p2_config).read_text(encoding="utf-8")) or {}
            target_cfg = phase2_data.get("target_selection", {})
            default_target = float(target_cfg.get("min_accepted_per_lv2", 30))
            targets = target_cfg.get("targets_by_lv2", {}) or {}
            known = {r["taxonomy_lv2"] for r in taxonomy_rows}
            for item in taxonomy_data.get("policies", []):
                lv2 = item.get("taxonomy_lv2") or item.get("id")
                if lv2 and lv2 not in known:
                    taxonomy_rows.append({"taxonomy_lv1": item.get("taxonomy_lv1", ""), "taxonomy_lv2": lv2,
                                          "total_accepted": 0, "phase1": 0, "phase2": 0,
                                          "tokens": 0, "cost_usd": 0.0})
            for row in taxonomy_rows:
                row["target"] = float(targets.get(row["taxonomy_lv2"], default_target))
                row["shortfall"] = max(0.0, row["target"] - float(row["total_accepted"] or 0))
            taxonomy_rows.sort(key=lambda row: (-row["shortfall"], row["taxonomy_lv2"]))
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Taxonomy 목표 설정을 읽지 못했습니다: {exc}")
        if taxonomy_rows:
            chosen_taxonomy = st.selectbox(
                "Taxonomy 선택", ["전체"] + [r["taxonomy_lv2"] for r in taxonomy_rows],
                help="선택하면 아래에서 해당 taxonomy의 상태·수집 단계·콘텐츠를 한 번에 확인합니다.",
            )
            coverage_table = [{
                "Taxonomy lv1": row["taxonomy_lv1"],
                "Taxonomy lv2": row["taxonomy_lv2"],
                "총 Accepted": row["total_accepted"],
                "1차 수집": row["phase1"],
                "2차 수집": row["phase2"],
                "목표 건수": int(row["target"]),
                "추가 필요": int(row["shortfall"]),
                "LLM 토큰": row["tokens"],
                "LLM 추정 비용": row["cost_usd"],
            } for row in taxonomy_rows]
            st.dataframe(coverage_table, width="stretch", hide_index=True,
                         column_config={"LLM 추정 비용": st.column_config.NumberColumn(format="$%.6f")})
    
            if chosen_taxonomy != "전체":
                selected_coverage = next(row for row in taxonomy_rows if row["taxonomy_lv2"] == chosen_taxonomy)
                st.dataframe([
                    {"수집 단계": "1차 수집", "Accepted": selected_coverage["phase1"]},
                    {"수집 단계": "2차 수집", "Accepted": selected_coverage["phase2"]},
                ], width="stretch", hide_index=True)
                st.caption(f"아래 원문 목록은 현재 선택한 DB(`{db_path}`)의 기록입니다.")
                st.dataframe(query(db_path, """
                    SELECT title,action,category,CASE WHEN collection_phase=2 THEN '2차' ELSE '1차' END AS phase,
                           ROUND(taxonomy_fit_score,2) AS taxonomy_fit,
                           ROUND(korea_relevance_score,2) AS "korea(본문 기준)",source_url
                    FROM content_records WHERE taxonomy_lv2=? AND COALESCE(is_supplementary,0)=0
                    ORDER BY action,rowid DESC LIMIT 200
                """, (chosen_taxonomy,)), width="stretch", hide_index=True,
                    column_config={"source_url": st.column_config.LinkColumn("URL", display_text="열기")})
        else:
            st.info("분류된 콘텐츠가 없습니다. 먼저 1차 수집을 실행하세요.")

    with overview:
        st.subheader("통합 · 전체 수집 요약")
        st.caption("1차 트렌드 수집과 2차 Tavily 보강 수집을 합산한 현황입니다. 단계별 상세는 각 전용 탭에서 확인하세요.")
        accepted_count = scalar(db_path, "SELECT COUNT(*) AS n FROM content_records WHERE action='accepted'")
        discard_count = scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status IN ('prefilter_discarded','discard')")
        cols = st.columns(4)
        for col, label, value in zip(
            cols,
            ["URL 후보", "저장 콘텐츠", "✅ accepted", "🗑️ discard"],
            [total_candidates, total_content, accepted_count, discard_count],
        ):
            col.metric(label, value)
    
        usage = query(db_path, """SELECT COALESCE(SUM(llm_input_tokens),0) AS input_tokens,
            COALESCE(SUM(llm_output_tokens),0) AS output_tokens,
            COALESCE(SUM(llm_total_tokens),0) AS total_tokens,
            COALESCE(SUM(llm_estimated_cost_usd),0) AS cost FROM url_candidates""")[0]
        u1, u2, u3 = st.columns(3)
        u1.metric("LLM 입력 토큰", f"{usage['input_tokens']:,}")
        u2.metric("LLM 출력 토큰", f"{usage['output_tokens']:,}")
        u3.metric("LLM 추정 비용", f"${usage['cost']:.6f}")
    
        st.subheader("Pipeline funnel")
        extracted = scalar(
            db_path,
            """SELECT COUNT(*) AS n FROM url_candidates
               WHERE status IN ('extracted','quality_failed','accepted','discard',
                                'candidate','duplicate','out_of_date_range',
                                'accepted','trend_excluded','trend_pending','discard')""",
        )
        accepted_final = scalar(
            db_path,
            "SELECT COUNT(*) AS n FROM url_candidates WHERE status='accepted'")
        st.bar_chart([
            {"stage": "discovered", "count": total_candidates},
            {"stage": "no-crawl", "count": scalar(
                db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status LIKE 'prefilter_%'")},
            {"stage": "extracted", "count": extracted},
            {"stage": "accepted", "count": accepted_final},
            {"stage": "stored", "count": total_content},
        ], x="stage", y="count")
    
        left, right = st.columns(2)
        with left:
            st.subheader("최종 상태")
            # DB 전체를 세면 설정을 바꾸기 전 실행 기록이 섞여, 지금은 꺼진 관문이
            # 아직 콘텐츠를 버리는 것처럼 보인다(실측: rerank_skipped 315건이 그랬다).
            _runs = query(db_path, "SELECT DISTINCT run_id FROM url_candidates "
                                   "WHERE COALESCE(run_id,'')!='' ORDER BY run_id DESC")
            _run_opts = ["최근 실행"] + [r["run_id"] for r in _runs] + ["전체 누적"]
            _pick = st.selectbox("집계 범위", _run_opts, key="funnel_run_scope")
            if _pick == "전체 누적" or not _runs:
                _sql, _args = ("SELECT status,COUNT(*) AS count FROM url_candidates "
                               "GROUP BY status ORDER BY count DESC"), ()
            else:
                _rid = _runs[0]["run_id"] if _pick == "최근 실행" else _pick
                _sql = ("SELECT status,COUNT(*) AS count FROM url_candidates WHERE run_id=? "
                        "GROUP BY status ORDER BY count DESC")
                _args = (_rid,)
                st.caption(f"실행 `{_rid}`")
            _rows = query(db_path, _sql, _args)
            for _r in _rows:
                _r["status"] = STATUS_LABEL.get(_r["status"], _r["status"])
            st.dataframe(_rows, width="stretch", hide_index=True)
        with right:
            st.subheader("Collection type")
            st.dataframe(query(
                db_path,
                """SELECT COALESCE(collection_type,'unknown') AS collection_type,COUNT(*) AS count
                   FROM url_candidates GROUP BY collection_type ORDER BY count DESC""",
            ), width="stretch", hide_index=True)
    
        # 트렌드 모드 집계 (keyword 모드에선 비어 있음)
        if scalar(db_path, "SELECT COUNT(*) AS n FROM content_records WHERE source!=''"):
            st.subheader("트렌드 수집")
            t1, t2, t3 = st.columns(3)
            with t1:
                st.caption("Source별")
                st.dataframe(query(
                    db_path,
                    "SELECT source,COUNT(*) AS count FROM content_records WHERE source!='' GROUP BY source ORDER BY count DESC",
                ), width="stretch", hide_index=True)
            with t2:
                st.caption("Action별")
                st.dataframe(query(
                    db_path,
                    "SELECT action,COUNT(*) AS count FROM content_records WHERE action IS NOT NULL AND action!='' GROUP BY action ORDER BY count DESC",
                ), width="stretch", hide_index=True)
            with t3:
                st.caption("risk/trend 평균")
                st.dataframe(query(
                    db_path,
                    """SELECT ROUND(AVG(risk_score),2) AS avg_risk, ROUND(AVG(trend_score),2) AS avg_trend,
                              ROUND(AVG(confidence),2) AS avg_conf
                       FROM content_records WHERE is_risk_candidate=1""",
                ), width="stretch", hide_index=True)
    
            # collection_type = 주제 버킷(gender_conflict 등), board_name = 갤러리명
            b1, b2 = st.columns(2)
            with b1:
                st.caption("주제 버킷별 (collection_type)")
                st.dataframe(query(
                    db_path,
                    "SELECT collection_type AS 주제버킷,COUNT(*) AS count FROM content_records "
                    "WHERE source!='' GROUP BY collection_type ORDER BY count DESC",
                ), width="stretch", hide_index=True)
            with b2:
                st.caption("갤러리/게시판별 (board_name)")
                st.dataframe(query(
                    db_path,
                    "SELECT board_name AS 갤러리,COUNT(*) AS count FROM content_records "
                    "WHERE board_name!='' GROUP BY board_name ORDER BY count DESC",
                ), width="stretch", hide_index=True)

    with content_tab:
        st.subheader("통합 · 저장 콘텐츠 탐색")
        st.caption("1차 content.db와 2차 phase2_pilot.db의 최종 accepted 콘텐츠를 함께 봅니다. 행을 선택하면 해당 DB의 정제 본문을 엽니다.")
    
        def _accepted_rows_for_explorer(record_db_path: str, db_label: str) -> list[dict]:
            """1차 source와 2차 원문 사이트(site_name)의 저장 위치 차이를 화면에서 통일한다."""
            if not Path(record_db_path).exists():
                return []
            rows = query(record_db_path, """
                SELECT content_id,title,source_url,canonical_url,source,site_name,domain,discovery_provider,
                       taxonomy_lv1,taxonomy_lv2,COALESCE(category, subtype) AS content_type,
                       action,collection_phase,collected_at
                FROM content_records
                WHERE action='accepted' AND COALESCE(is_supplementary,0)=0
            """)
            site_labels = {
                "naver_kin": "네이버 지식인", "doctornow": "닥터나우", "instiz": "인스티즈",
                "yonhap_news": "연합뉴스", "dcinside": "디시인사이드", "ilbe": "일간베스트",
            }
            for row in rows:
                # provider는 URL을 찾은 방식일 뿐 수집원이 아니다. 2차는 실제 원문 사이트를 우선 표기한다.
                row["수집원"] = row["source"] or site_labels.get(row["site_name"], row["domain"] or "unknown")
                row["수집 단계"] = "2차 taxonomy 수집" if row["collection_phase"] == 2 else "1차 수집"
                row["_db_path"] = record_db_path
                row["_db_label"] = db_label
            return rows
    
        explorer_rows = (
            _accepted_rows_for_explorer(paths.DB_DEFAULT, "content.db")
            + _accepted_rows_for_explorer(paths.PHASE2_DB_DEFAULT, "phase2_pilot.db")
        )
        # 동일 URL이 두 DB에 있을 땐 2차 기록을 우선하고 한 번만 표시한다.
        deduped_rows: dict[str, dict] = {}
        for row in sorted(explorer_rows, key=lambda r: (r["collection_phase"] == 2, r["collected_at"] or ""), reverse=True):
            deduped_rows.setdefault(row["canonical_url"] or row["source_url"] or row["content_id"], row)
        explorer_rows = list(deduped_rows.values())
    
        mc = st.columns(2)
        mc[0].metric("최종 accepted", len(explorer_rows))
        mc[1].metric("Taxonomy 매핑 완료", sum(bool(row["taxonomy_lv2"]) for row in explorer_rows))
    
        # ── taxonomy 분포 차트 (매핑된 콘텐츠) ──
        dist = (
            pd.DataFrame(explorer_rows).dropna(subset=["taxonomy_lv2"]).groupby("taxonomy_lv2").size()
            .reset_index(name="count").rename(columns={"taxonomy_lv2": "lv2"}).sort_values("count", ascending=False)
            .to_dict("records") if explorer_rows else []
        )
        if dist:
            st.caption("Taxonomy lv2 분포 (매핑된 콘텐츠)")
            st.bar_chart(dist, x="lv2", y="count", horizontal=True)
    
        # ── 필터 ──
        lv2_rows = sorted({row["taxonomy_lv2"] for row in explorer_rows if row["taxonomy_lv2"]})
        src_rows = sorted({row["수집원"] for row in explorer_rows if row["수집원"] != "unknown"})
        f1, f2, f3 = st.columns(3)
        lv2 = f1.selectbox("Taxonomy lv2", ["전체"] + lv2_rows)
        source_sel = f2.selectbox("수집원", ["전체"] + src_rows)
        content_phase = f3.selectbox("수집 단계", ["전체", "1차 수집", "2차 taxonomy 수집"], key="content_phase_filter")
        records = [
            row for row in explorer_rows
            if (lv2 == "전체" or row["taxonomy_lv2"] == lv2)
            and (source_sel == "전체" or row["수집원"] == source_sel)
            and (content_phase == "전체" or row["수집 단계"] == content_phase)
        ]
        records.sort(key=lambda row: row["collected_at"] or "", reverse=True)
        records = records[:300]
        st.caption(f"{len(records)}건")
        table = [{"제목": r["title"] or "(제목 없음)", "수집원": r["수집원"], "수집 단계": r["수집 단계"], "URL": r["source_url"],
                  "Taxonomy lv1": r["taxonomy_lv1"] or "—", "Taxonomy lv2": r["taxonomy_lv2"] or "—",
                  "Type": r["content_type"] or "—", "처리 상태": ACTION_LABEL.get(r["action"], r["action"])} for r in records]
        st.caption("표의 행을 클릭하면 아래에 상세 내용이 열립니다.")
        table_event = st.dataframe(
            table, width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row",
            key="content_record_table",
            column_config={"URL": st.column_config.LinkColumn("URL", display_text="열기")},
        )
        selected_rows = table_event.selection.rows
        selected_index = selected_rows[0] if selected_rows else None
    
        # ── 상세 카드 ──
        if records and selected_index is not None and selected_index < len(records):
            selected_id = records[selected_index]["content_id"]
            detail_db_path = records[selected_index]["_db_path"]
            has_core_text = has_column(detail_db_path, "content_records", "core_text")
            qa_fields = "question_body,answer_body," if has_core_text else "'' AS question_body,'' AS answer_body,"
            display_field = "COALESCE(NULLIF(core_text,''), body_text) AS display_text" if has_core_text else "body_text AS display_text"
            detail = query(
                detail_db_path,
                f"""SELECT title,source_url,{qa_fields}
                          {display_field},filter_reason,
                          taxonomy_lv1,taxonomy_lv2,category,action,risk_score,trend_score,confidence,is_risk_candidate,
                          risk_signals,secondary_flags,matched_keywords,classification_source,classification_reason,
                          harmfulness_score,taxonomy_fit_score,korean_language_ratio,korea_relevance_score,
                          korea_context_evidence,concrete_context_score,
                          is_harmful,evidence_spans,
                          llm_model,llm_input_tokens,llm_output_tokens,llm_total_tokens,llm_estimated_cost_usd,
                          source,board_name,collection_type,extractor,published_at
                   FROM content_records WHERE content_id=?""",
                (selected_id,),
            )[0]
    
            def _jl(v):   # JSON list 컬럼 안전 파싱
                try:
                    return json.loads(v) if v else []
                except (json.JSONDecodeError, TypeError):
                    return []
    
            st.markdown(f"### {detail['title'] or '(제목 없음)'}")
            if detail["taxonomy_lv2"]:
                st.markdown(f"🏷️ **{detail['taxonomy_lv1']} → {detail['taxonomy_lv2']} → {detail['category']}**  ·  "
                            f"{ACTION_LABEL.get(detail['action'], detail['action'])}  ·  "
                            f"분류: `{detail['classification_source']}`")
                if detail["classification_reason"]:
                    st.caption(f"Taxonomy 판정 근거 · {detail['classification_reason']}")
                score_cols = st.columns(4)
                score_labels = [
                    ("Taxonomy fit", " Taxonomy와의 적합도", detail["taxonomy_fit_score"]),
                    ("Harmfulness", " 유해성 강도", detail["harmfulness_score"]),
                    ("Korea context", "한국 관련 맥락 — 본문 전체 기준", detail["korea_relevance_score"]),
                    ("Concrete context", " 실제 사례·행위 등 구체적 맥락", detail["concrete_context_score"]),
                ]
                for column, (label, description, score) in zip(score_cols, score_labels):
                    column.markdown(
                        f"**{label}**  \n<span style='color: #6b7280'>: {description}</span>\n\n## {score}",
                        unsafe_allow_html=True,
                    )
                evidence = _jl(detail["evidence_spans"])
                if evidence:
                    st.caption("판정 근거 문구: " + " · ".join(f"‘{x}’" for x in evidence))
                korea_evidence = _jl(detail["korea_context_evidence"])
                st.caption(
                    f"본문 한글 비율: {detail['korean_language_ratio']} · "
                    f"한국 맥락 근거: {' · '.join(korea_evidence) if korea_evidence else '감지되지 않음'}"
                )
            else:
                st.info(f"매핑 안 됨 · {ACTION_LABEL.get(detail['action'], detail['action'])} "
                        f"(위험신호 후보={'예' if detail['is_risk_candidate'] else '아니오'})")
            st.caption(
                f"{detail['source'] or '?'} · {detail['board_name'] or ''} · {detail['extractor']} · "
                f"{detail['published_at'] or '날짜 없음'}  ·  [원문 열기]({detail['source_url']})"
            )
            with st.expander("핵심 본문", expanded=True):
                st.write(detail["display_text"] or "(본문 없음)")
            if detail["question_body"] or detail["answer_body"]:
                with st.expander("Q&A 구조 보기", expanded=False):
                    if detail["question_body"]:
                        st.markdown("**질문**")
                        st.write(detail["question_body"])
                    if detail["answer_body"]:
                        st.markdown("**답변**")
                        st.write(detail["answer_body"])
            if detail["llm_total_tokens"]:
                st.caption(
                    f"LLM `{detail['llm_model']}` · 입력 {detail['llm_input_tokens']:,} · "
                    f"출력 {detail['llm_output_tokens']:,} · 합계 {detail['llm_total_tokens']:,} tokens · "
                    f"추정 ${detail['llm_estimated_cost_usd']:.8f}"
                )
            if detail["filter_reason"]:
                st.caption(f"판정 사유: `{detail['filter_reason']}`")
    
            linked = query(
                detail_db_path,
                """SELECT title,source_url,body_text,link_source
                   FROM content_records
                   WHERE is_supplementary=1 AND parent_source_url=?
                   ORDER BY rowid""",
                (detail["source_url"],),
            )
            linked_candidates = query(
                detail_db_path,
                """SELECT source_url,status,
                          CASE WHEN status='extraction_failed' THEN COALESCE(
                            (SELECT reason FROM filter_logs f WHERE f.source_url=url_candidates.source_url
                             AND f.stage='extract' ORDER BY f.id DESC LIMIT 1), filter_reason)
                          ELSE filter_reason END AS filter_reason,
                          link_source
                   FROM url_candidates
                   WHERE is_supplementary=1 AND parent_source_url=?
                     AND status!='supplementary_collected'
                   ORDER BY rowid""",
                (detail["source_url"],),
            )
            if linked or linked_candidates:
                with st.expander(f"🔗 보조 콘텐츠 {len(linked)}개", expanded=True):
                    for item in linked:
                        st.markdown(
                            f"**{item['title'] or '(제목 없음)'}** · {item['link_source'] or 'body'} · "
                            f"[원문 열기]({item['source_url']})"
                        )
                        st.write(item["body_text"] or "(추출된 텍스트 없음)")
                    for item in linked_candidates:
                        st.caption(
                            f"수집 실패/제외 · {item['link_source'] or 'body'} · "
                            f"`{item['status']}` · `{item['filter_reason'] or ''}` · {item['source_url']}"
                        )

    with candidates_tab:
        st.subheader("통합 · 후보 URL 처리 현황")
        st.caption("1·2차 후보를 함께 봅니다. 아래에서 수집 단계를 고르면 해당 단계만 확인할 수 있습니다.")
        stage_cols = st.columns(4)
        stage_cols[0].metric("제목 단계 제외", scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status='prefilter_discarded'"))
        stage_cols[1].metric("수집 실패", scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status='extraction_failed'"))
        stage_cols[2].metric("본문·LLM 제외", scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status IN ('discard','trend_excluded','duplicate')"))
        stage_cols[3].metric("최종 확보", scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status='accepted'"))
        statuses = ["전체"] + [row["status"] for row in query(
            db_path, "SELECT DISTINCT status FROM url_candidates WHERE status IS NOT NULL "
            "AND status <> 'supplementary_collected' ORDER BY status")]
        methods = ["전체"] + [row["discovery_method"] for row in query(
            db_path,
            "SELECT DISTINCT discovery_method FROM url_candidates "
            "WHERE discovery_method IS NOT NULL AND discovery_method <> 'in_body_link' "
            "ORDER BY discovery_method")]
        c1, c2, c3 = st.columns(3)
        status = c1.selectbox("처리 결과", statuses, format_func=lambda x: STATUS_LABEL.get(x, x))
        method = c2.selectbox("발견 경로", methods, format_func=lambda x: METHOD_LABEL.get(x, x))
        candidate_phase = c3.selectbox("수집 단계", ["전체", "① 1차 수집", "② Tavily 2차"], key="candidate_phase_filter")
        # 본문 속 링크 추종은 폐지된 기능이라 과거 이력을 화면에 노출하지 않는다(DB에는 남긴다).
        where, params = ["COALESCE(discovery_method,'') <> 'in_body_link'"], []
        if status != "전체":
            where.append("status=?"); params.append(status)
        if method != "전체":
            where.append("discovery_method=?"); params.append(method)
        if candidate_phase == "① 1차 수집":
            where.append("COALESCE(collection_phase,1)=1")
        elif candidate_phase == "② Tavily 2차":
            where.append("collection_phase=2")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = query(
            db_path,
            f"""SELECT title,source,category_name,source_url,taxonomy_lv2_candidate,subtype_candidate,collection_type,
                       discovery_method,search_api,status,ROUND(value_score,3) AS value_score,
                       CASE WHEN status='extraction_failed' THEN COALESCE(
                         (SELECT reason FROM filter_logs f WHERE f.source_url=url_candidates.source_url
                          AND f.stage='extract' ORDER BY f.id DESC LIMIT 1), filter_reason)
                       ELSE filter_reason END AS filter_reason,
                       parent_source_url,link_source
                FROM url_candidates {clause}
                ORDER BY rowid DESC LIMIT 500""",
            tuple(params),
        )
        friendly_rows = [{
            "제목": row["title"] or "(제목 없음)",
            "수집원": row["source"] or "—",
            "분야": row["category_name"] or row["collection_type"] or "—",
            "처리 결과": STATUS_LABEL.get(row["status"], row["status"]),
            "사유": row["filter_reason"] or "—",
            "발견 경로": METHOD_LABEL.get(row["discovery_method"], row["discovery_method"]),
            "URL": row["source_url"],
            "내부 상태": row["status"],
        } for row in rows]
        st.dataframe(friendly_rows, width="stretch", hide_index=True,
                     column_config={"URL": st.column_config.LinkColumn("URL", display_text="열기")})

    with llm_tab:
        st.subheader("공통 · LLM 토큰 및 추정 비용")
        st.caption("API usage 기반이며, 설정된 모델 단가로 계산한 추정치입니다. 실제 청구액은 OpenAI 결제 내역이 기준입니다.")
        totals = query(db_path, """SELECT COALESCE(llm_model,'unknown') AS model,
            COUNT(*) AS calls, COALESCE(SUM(llm_input_tokens),0) AS input_tokens,
            COALESCE(SUM(llm_cached_input_tokens),0) AS cached_tokens,
            COALESCE(SUM(llm_output_tokens),0) AS output_tokens,
            COALESCE(SUM(llm_total_tokens),0) AS total_tokens,
            ROUND(COALESCE(SUM(llm_estimated_cost_usd),0),8) AS estimated_cost_usd
            FROM url_candidates WHERE llm_total_tokens>0 GROUP BY llm_model ORDER BY estimated_cost_usd DESC""")
        st.dataframe(totals, width="stretch", hide_index=True)
        cost_left, cost_right = st.columns(2)
        with cost_left:
            st.caption("수집 단계별")
            st.dataframe(query(db_path, """
                SELECT CASE WHEN collection_phase=2 THEN '2차 보강' ELSE '1차 수집' END AS phase,
                       COUNT(*) AS calls,COALESCE(SUM(llm_total_tokens),0) AS total_tokens,
                       ROUND(COALESCE(SUM(llm_estimated_cost_usd),0),6) AS estimated_cost_usd
                FROM url_candidates WHERE llm_total_tokens>0 GROUP BY phase ORDER BY phase
            """), width="stretch", hide_index=True)
        with cost_right:
            st.caption("대상 Taxonomy별 (2차는 검색 target 기준)")
            st.dataframe(query(db_path, """
                SELECT COALESCE(taxonomy_lv2_candidate,'미지정') AS taxonomy,
                       COUNT(*) AS calls,COALESCE(SUM(llm_total_tokens),0) AS total_tokens,
                       ROUND(COALESCE(SUM(llm_estimated_cost_usd),0),6) AS estimated_cost_usd
                FROM url_candidates WHERE llm_total_tokens>0
                GROUP BY taxonomy_lv2_candidate ORDER BY estimated_cost_usd DESC LIMIT 50
            """), width="stretch", hide_index=True)
        st.subheader("저장 콘텐츠별 사용량")
        st.dataframe(query(db_path, """SELECT title,action,taxonomy_lv2,category,llm_model,
            llm_input_tokens,llm_cached_input_tokens,llm_output_tokens,llm_total_tokens,
            ROUND(llm_estimated_cost_usd,8) AS estimated_cost_usd,source_url
            FROM content_records WHERE llm_total_tokens>0 ORDER BY rowid DESC LIMIT 500"""),
            width="stretch", hide_index=True,
            column_config={"source_url": st.column_config.LinkColumn("URL", display_text="열기")})

    with failures_tab:
        st.subheader("공통 · 단계·사유별 실패")
        st.caption("1·2차 수집 과정의 실패 이력을 함께 보여줍니다.")
        st.dataframe(query(
            db_path,
            """SELECT stage,reason,COUNT(*) AS count FROM filter_logs
               WHERE status='fail' GROUP BY stage,reason ORDER BY count DESC LIMIT 300""",
        ), width="stretch", hide_index=True)
        st.subheader("사이트별 추출 실패")
        st.dataframe(query(
            db_path,
            """SELECT domain,COUNT(*) AS count FROM url_candidates
               WHERE status='extraction_failed' GROUP BY domain ORDER BY count DESC""",
        ), width="stretch", hide_index=True)
