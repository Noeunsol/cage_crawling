"""크롤러 SQLite를 읽기 전용으로 관찰하는 Streamlit 대시보드."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import streamlit as st
import yaml
from dotenv import load_dotenv

load_dotenv()

# 최종 판정: accepted/excluded/pending · 1차: keep/discard
_ACTION_LABEL = {
    "accepted": "✅ accepted", "review": "🟨 review",
    "excluded": "⛔ excluded", "pending": "⏳ pending",
    "keep": "📥 keep", "discard": "🗑️ discard",
}
_STATUS_LABEL = {
    "prefilter_discarded": "제목 단계 제외",
    "extraction_failed": "본문 수집 실패",
    "trend_discard": "본문 확인 후 제외",
    "trend_excluded": "LLM 분류 후 제외",
    "trend_accepted": "최종 채택",
    "trend_review": "검토 필요",
    "trend_pending": "LLM 처리 실패·재시도 필요",
    "duplicate": "중복 콘텐츠",
    "supplementary_collected": "보조 링크 수집 완료",
    "discovered": "발견",
}
_METHOD_LABEL = {
    "board_list": "게시판 목록",
    "rss": "뉴스 RSS",
    "in_body_link": "본문·댓글 내부 링크",
}

st.set_page_config(page_title="CAGE Crawler Monitor", page_icon="🕸️", layout="wide")
st.title("CAGE Crawler Monitor")
st.caption("후보 발견부터 저장까지 확인하는 read-only 대시보드 · raw 원문은 표시하지 않습니다.")


@st.cache_data(ttl=5)
def query(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(db_path: str, sql: str, params: tuple = ()) -> int:
    rows = query(db_path, sql, params)
    return next(iter(rows[0].values())) if rows else 0


def _target_rows(stats: dict, preview: bool = False) -> list[dict]:
    """파이프라인의 source별 최종 usable 목표 통계를 UI용 표로 바꾼다."""
    if preview:
        return [{
            "수집원": source,
            "accepted 우선 목표": values.get("target", 0),
            "목록 후보": values.get("available", 0),
            "제목 통과 예상": values.get("title_keep", 0),
            "제목 제외 예상": values.get("prefilter_discard", 0),
        } for source, values in stats.items()]
    return [
        {
            "수집원": source,
            "accepted 우선 목표": values.get("target", 0),
            "후보 확인": values.get("scanned", 0),
            "제목 통과": values.get("title_keep", 0),
            "accepted": values.get("accepted", 0),
            "review": values.get("review", 0),
            "accepted 확보": values.get("accepted", 0),
            "제외·실패": sum(values.get(k, 0) for k in (
                "prefilter_discard", "content_discard", "extraction_failed",
                "excluded", "pending", "duplicate",
            )),
            "부족": values.get("shortfall", 0),
        }
        for source, values in stats.items()
    ]


db_path = st.sidebar.text_input("SQLite DB", "data/content.db")
refresh_col, env_col = st.sidebar.columns(2)
if refresh_col.button("새로고침"):
    st.cache_data.clear()
if env_col.button(".env 다시 읽기"):
    load_dotenv(override=True)
    st.cache_data.clear()
    st.rerun()

with st.sidebar.expander("▶ 트렌드 수집 실행", expanded=not Path(db_path).is_file()):
    st.markdown("**수집원 · 목표 건수**")
    d1, d2 = st.columns([1.2, 1])
    use_dc = d1.checkbox("디시인사이드", value=True)
    dc_target = d2.number_input("디시 건수", 10, 2000, 500, step=50,
                                disabled=not use_dc, key="dc_t", label_visibility="collapsed")
    n1, n2 = st.columns([1.2, 1])
    use_news = n1.checkbox("뉴스 RSS", value=True)
    news_target = n2.number_input("뉴스 건수", 10, 2000, 300, step=50,
                                  disabled=not use_news, key="news_t", label_visibility="collapsed")
    st.caption("FM코리아·네이트판은 다음 단계 지원 예정")

    total_target = (int(dc_target) if use_dc else 0) + (int(news_target) if use_news else 0)
    st.metric("전체 accepted 목표", f"{total_target:,}건")
    st.caption("디시·뉴스 입력값은 우선 배분입니다. 한쪽이 부족하면 다른 수집원이 보충하며 accepted만 전체 목표에 포함합니다.")
    st.caption("본문 키워드 룰로 재탈락시키지 않으며, OCR 없이 제목과 HTML 본문을 LLM이 최종 분류합니다.")
    reset = st.checkbox("기존 DB 비우고 새로 수집")

    if st.checkbox("고급 설정"):
        trend_cfg = st.text_input("trend config", "configs/trend_collection.yaml")
        taxo_cfg = st.text_input("taxonomy", "configs/taxonomy.yaml")
        settings_cfg = st.text_input("crawler settings", "configs/crawler_settings.yaml")
    else:
        trend_cfg, taxo_cfg, settings_cfg = (
            "configs/trend_collection.yaml", "configs/taxonomy.yaml", "configs/crawler_settings.yaml"
        )

    try:
        settings_data = yaml.safe_load(Path(settings_cfg).read_text(encoding="utf-8"))
    except Exception:
        settings_data = {}
    try:
        trend_data = yaml.safe_load(Path(trend_cfg).read_text(encoding="utf-8"))
    except Exception:
        trend_data = {}
    comment_defaults = (
        settings_data.get("extraction", {}).get("comments", {}).get("relevance_filter", {})
    )

    with st.expander("댓글 정제 설정"):
        drop_duplicates = st.checkbox(
            "중복 댓글 제거", value=comment_defaults.get("drop_duplicates", True)
        )
        drop_unrelated = st.checkbox(
            "본문과 무관한 댓글 제거", value=comment_defaults.get("drop_unrelated", True)
        )
        comment_min_score = st.slider(
            "관련성 최소 점수", 0.0, 1.0,
            float(comment_defaults.get("min_score", 0.08)), 0.01,
            disabled=not drop_unrelated,
        )
        st.caption("위험 신호·피해 증언·반박·정정 댓글은 점수가 낮아도 보존합니다. 댓글은 LLM에 전달하지 않습니다.")

    with st.expander("탐색 범위 설정"):
        scan_multiplier = st.slider(
            "목표 대비 최대 후보 배수", 1, 30,
            int(trend_data.get("discovery_limits", {}).get("max_scan_multiplier", 10)),
        )
        st.caption(
            f"현재 소스별 목표의 최대 {scan_multiplier}배 후보를 확인합니다. "
            "값이 크면 accepted 확보 가능성과 실행시간·LLM 비용이 함께 증가합니다."
        )

    with st.expander("LLM 분류 상태"):
        try:
            llm_cfg = settings_data["matching"]
            provider = llm_cfg.get("llm", {}).get("provider", "anthropic")
            model = llm_cfg.get("llm", {}).get("model", "")
            env_name = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
            ready = bool(os.getenv(env_name))
            if ready:
                st.success("LLM 분류 준비 완료")
            else:
                st.error("LLM API 키가 없습니다")
            st.write(f"Provider: `{provider}` · Model: `{model}`")
            st.write("공통 classifier 1회 · 19개 Lv2 Definition/Description 전체 비교")
            st.caption(
                f"accepted: confidence ≥ {llm_cfg.get('accepted_confidence', 0.75)}, "
                f"harmfulness ≥ {llm_cfg.get('accepted_harmfulness', 0.60)}, "
                f"concrete context ≥ {llm_cfg.get('accepted_concrete_context', 0.50)}"
            )
            if not ready:
                st.code(f'{env_name}="sk-..."', language="bash")
                st.caption("프로젝트 루트의 .env에 추가한 뒤 '.env 다시 읽기'를 누르세요. 키 값은 화면과 DB에 저장하지 않습니다.")
        except Exception as exc:  # noqa: BLE001
            st.caption(f"분류 설정 확인 실패: {exc}")

    overrides = {
        "target_by_source": {"dcinside": int(dc_target), "news_rss": int(news_target)},
        "enabled": {"dcinside": use_dc, "news_rss": use_news},
        "discovery_limits": {"max_scan_multiplier": int(scan_multiplier)},
        "comment_filter": {
            "enabled": True,
            "drop_duplicates": drop_duplicates,
            "drop_unrelated": drop_unrelated,
            "min_score": comment_min_score,
        },
    }

    b1, b2 = st.columns(2)
    if b1.button("미리보기"):
        with st.spinner("가용 규모 확인 중…"):
            try:
                from src import pipeline
                rep = pipeline.run_trend(trend_config=trend_cfg, taxonomy_config=taxo_cfg,
                                         settings_config=settings_cfg, db_path=db_path,
                                         dry_run=True, overrides=overrides)
                st.success(f"제목 단계 상세수집 후보 약 {rep.get('would_extract', 0):,}건")
                st.caption("미리보기는 본문과 LLM을 호출하지 않으므로 최종 accepted 수는 실제 실행 후 확정됩니다.")
                if rep.get("collection_targets"):
                    st.dataframe(_target_rows(rep["collection_targets"], preview=True), hide_index=True)
                st.json(rep.get("by_source", {}))
            except Exception as exc:  # noqa: BLE001 (UI 표시)
                st.error(f"실패: {exc}")

    if b2.button("수집 시작", type="primary"):
        if total_target == 0:
            st.warning("수집원을 하나 이상 선택하세요.")
        else:
            bar = st.progress(0.0, text="목록 수집 중…")

            def _prog(done, total):
                bar.progress(done / total if total else 1.0, text=f"{done}/{total}건 처리 중…")

            try:
                from src import pipeline
                rep = pipeline.run_trend(trend_config=trend_cfg, taxonomy_config=taxo_cfg,
                                         settings_config=settings_cfg, db_path=db_path, reset_db=reset,
                                         overrides=overrides, on_progress=_prog)
                bar.progress(1.0, text="완료")
                st.cache_data.clear()
                st.success(f"저장 {rep.get('stored_records', 0):,}건 · 아래 탭에서 확인")
                if rep.get("by_action"):
                    st.caption("최종 분류 결과 (accepted/review/excluded/pending)")
                    st.json(rep["by_action"])
                if rep.get("collection_targets"):
                    st.caption("accepted 목표 달성 현황")
                    st.dataframe(_target_rows(rep["collection_targets"]), hide_index=True)
                goal = rep.get("collection_goal", {})
                if goal:
                    st.info(
                        f"전체 accepted {goal.get('accepted', 0)}/{goal.get('total_target', total_target)} · "
                        f"review {goal.get('review', 0)} · 부족 {goal.get('shortfall', 0)}"
                    )
            except Exception as exc:  # noqa: BLE001 (UI 표시)
                st.error(f"실행 실패: {exc}")

if not Path(db_path).is_file():
    st.info(f"`{db_path}`가 아직 없습니다. 사이드바 **'▶ 트렌드 수집 실행'** 에서 시작하거나 CLI로 크롤러를 돌린 뒤 새로고침하세요.")
    st.stop()

# 구 스키마 DB를 현재 컬럼으로 맞춘다(비파괴 ADD COLUMN). 신규 컬럼(risk_signals 등) 조회 가능하게.
try:
    from src.store import Store
    Store(db_path).close()
except Exception as exc:  # noqa: BLE001 (마이그레이션 실패해도 조회는 시도)
    st.warning(f"스키마 자동 정렬 건너뜀: {exc}")

try:
    total_candidates = scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates")
    total_content = scalar(db_path, "SELECT COUNT(*) AS n FROM content_records")
except sqlite3.Error as exc:
    st.error(f"DB를 읽을 수 없습니다: {exc}")
    st.stop()

overview, prefilter_tab, candidates_tab, content_tab, llm_tab, failures_tab, pii_tab = st.tabs(
    ["Overview", "Pre-filter", "URL Candidates", "Content Explorer", "LLM Usage", "Failure Analysis", "PII"]
)

with overview:
    accepted_count = scalar(db_path, "SELECT COUNT(*) AS n FROM content_records WHERE action='accepted'")
    review_count = scalar(db_path, "SELECT COUNT(*) AS n FROM content_records WHERE action='review'")
    pending_count = scalar(db_path, "SELECT COUNT(*) AS n FROM content_records WHERE action='pending'")
    pii_count = scalar(db_path, "SELECT COUNT(*) AS n FROM content_records WHERE pii_detected=1")
    cols = st.columns(6)
    for col, label, value in zip(
        cols,
        ["URL 후보", "저장 콘텐츠", "✅ accepted", "🟨 review", "⏳ pending", "PII 탐지"],
        [total_candidates, total_content, accepted_count, review_count, pending_count, pii_count],
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
           WHERE status IN ('extracted','quality_failed','matched_pass','matched_review',
                            'matched_fail','duplicate','out_of_date_range',
                            'trend_accepted','trend_excluded','trend_pending','trend_discard')""",
    )
    accepted_or_pending = scalar(
        db_path,
        "SELECT COUNT(*) AS n FROM url_candidates WHERE status IN ('matched_pass','trend_accepted','trend_pending')")
    st.bar_chart([
        {"stage": "discovered", "count": total_candidates},
        {"stage": "no-crawl", "count": scalar(
            db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status LIKE 'prefilter_%'")},
        {"stage": "extracted", "count": extracted},
        {"stage": "accepted/pending", "count": accepted_or_pending},
        {"stage": "stored", "count": total_content},
    ], x="stage", y="count")

    left, right = st.columns(2)
    with left:
        st.subheader("최종 상태")
        st.dataframe(query(
            db_path,
            "SELECT status,COUNT(*) AS count FROM url_candidates GROUP BY status ORDER BY count DESC",
        ), width="stretch", hide_index=True)
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

with prefilter_tab:
    st.subheader("원문을 열지 않고 제외한 후보")
    st.caption("수집원과 관계없이 제목만 판단했습니다. 본문·댓글·RSS 요약은 이 단계에서 사용하거나 저장하지 않습니다.")
    pc = st.columns(3)
    pc[0].metric("크롤링 절약", scalar(
        db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status LIKE 'prefilter_%'"))
    pc[1].metric("Discarded", scalar(
        db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status='prefilter_discarded'"))
    pc[2].metric("Trend seed", scalar(
        db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status='prefilter_discarded' AND is_trend_seed=1"))

    actions = ["전체", "discarded", "trend seed"]
    sources = ["전체"] + [row["source"] for row in query(
        db_path, "SELECT DISTINCT source FROM url_candidates WHERE source IS NOT NULL AND source!='' ORDER BY source")]
    pf1, pf2 = st.columns(2)
    pf_action = pf1.selectbox("Pre-filter action", actions)
    pf_source = pf2.selectbox("Pre-filter 수집원", sources)
    where, params = ["status LIKE 'prefilter_%'"], []
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

with candidates_tab:
    st.subheader("후보 URL 처리 현황")
    st.caption("각 URL이 어느 단계까지 진행됐고 왜 채택·제외됐는지 보여줍니다. 내부 상태 코드는 표의 마지막 열에서 확인할 수 있습니다.")
    stage_cols = st.columns(4)
    stage_cols[0].metric("제목 단계 제외", scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status='prefilter_discarded'"))
    stage_cols[1].metric("수집 실패", scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status='extraction_failed'"))
    stage_cols[2].metric("본문·LLM 제외", scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status IN ('trend_discard','trend_excluded','duplicate')"))
    stage_cols[3].metric("최종 확보", scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status IN ('trend_accepted','trend_review')"))
    statuses = ["전체"] + [row["status"] for row in query(
        db_path, "SELECT DISTINCT status FROM url_candidates WHERE status IS NOT NULL ORDER BY status")]
    methods = ["전체"] + [row["discovery_method"] for row in query(
        db_path,
        "SELECT DISTINCT discovery_method FROM url_candidates WHERE discovery_method IS NOT NULL ORDER BY discovery_method")]
    c1, c2 = st.columns(2)
    status = c1.selectbox("처리 결과", statuses, format_func=lambda x: _STATUS_LABEL.get(x, x))
    method = c2.selectbox("발견 경로", methods, format_func=lambda x: _METHOD_LABEL.get(x, x))
    where, params = [], []
    if status != "전체":
        where.append("status=?"); params.append(status)
    if method != "전체":
        where.append("discovery_method=?"); params.append(method)
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
        "처리 결과": _STATUS_LABEL.get(row["status"], row["status"]),
        "사유": row["filter_reason"] or "—",
        "발견 경로": _METHOD_LABEL.get(row["discovery_method"], row["discovery_method"]),
        "URL": row["source_url"],
        "내부 상태": row["status"],
    } for row in rows]
    st.dataframe(friendly_rows, width="stretch", hide_index=True,
                 column_config={"URL": st.column_config.LinkColumn("URL", display_text="열기")})

with content_tab:
    # ── 요약 지표 ──
    def _n(where=""):
        return scalar(db_path, f"SELECT COUNT(*) AS n FROM content_records {where}")
    # 저장되는 최종 상태는 accepted(분류 완료) + pending(LLM 대기). excluded/discard는 후보 로그에만 남음.
    mc = st.columns(5)
    mc[0].metric("전체 저장", _n("WHERE COALESCE(is_supplementary,0)=0"))
    mc[1].metric("✅ accepted", _n("WHERE action='accepted' AND COALESCE(is_supplementary,0)=0"))
    mc[2].metric("🟨 review", _n("WHERE action='review' AND COALESCE(is_supplementary,0)=0"))
    mc[3].metric("⏳ pending(LLM 대기)", _n("WHERE action='pending' AND COALESCE(is_supplementary,0)=0"))
    mc[4].metric("🔗 보조 콘텐츠", _n("WHERE is_supplementary=1"))

    # ── taxonomy 분포 차트 (매핑된 콘텐츠) ──
    dist = query(db_path, """SELECT taxonomy_lv2 AS lv2, COUNT(*) AS count FROM content_records
                             WHERE taxonomy_lv2 IS NOT NULL GROUP BY taxonomy_lv2 ORDER BY count DESC""")
    if dist:
        st.caption("Taxonomy lv2 분포 (매핑된 콘텐츠)")
        st.bar_chart(dist, x="lv2", y="count", horizontal=True)

    # ── 필터 ──
    lv2_rows = query(
        db_path, "SELECT DISTINCT taxonomy_lv2 FROM content_records WHERE taxonomy_lv2 IS NOT NULL ORDER BY taxonomy_lv2")
    src_rows = query(
        db_path, "SELECT DISTINCT source FROM content_records WHERE source!='' ORDER BY source")
    f1, f2, f3 = st.columns(3)
    lv2 = f1.selectbox("Taxonomy lv2", ["전체"] + [row["taxonomy_lv2"] for row in lv2_rows])
    source_sel = f2.selectbox("수집원", ["전체"] + [row["source"] for row in src_rows])
    action_sel = f3.selectbox("처리 상태", ["전체", "accepted", "review", "pending"])
    where, params = ["COALESCE(is_supplementary,0)=0"], []
    if lv2 != "전체":
        where.append("taxonomy_lv2=?"); params.append(lv2)
    if source_sel != "전체":
        where.append("source=?"); params.append(source_sel)
    if action_sel != "전체":
        where.append("action=?"); params.append(action_sel)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    records = query(
        db_path,
        f"""SELECT content_id,title,source_url,source,taxonomy_lv1,taxonomy_lv2,category,action,
                   risk_score,trend_score,confidence,
                   ROUND(pii_risk_score,3) AS pii_risk
            FROM content_records {clause} ORDER BY collected_at DESC LIMIT 300""",
        tuple(params),
    )
    st.caption(f"{len(records)}건")
    table = [{"제목": r["title"] or "(제목 없음)", "수집원": r["source"], "URL": r["source_url"],
              "lv1": r["taxonomy_lv1"] or "—", "lv2": r["taxonomy_lv2"] or "—", "type": r["category"] or "—",
              "처리": _ACTION_LABEL.get(r["action"], r["action"]),
              "risk": r["risk_score"], "trend": r["trend_score"], "conf": r["confidence"],
              "pii": r["pii_risk"]} for r in records]
    st.dataframe(table, width="stretch", hide_index=True,
                 column_config={"URL": st.column_config.LinkColumn("URL", display_text="열기")})

    # ── 상세 카드 ──
    if records:
        options = {f"{r['title'] or '(제목 없음)'}  ·  {r['content_id'][:8]}": r["content_id"] for r in records}
        selected = st.selectbox("🔎 상세 보기", options)
        detail = query(
            db_path,
            """SELECT title,source_url,masked_text,masked_comments,filter_reason,
                      taxonomy_lv1,taxonomy_lv2,category,action,risk_score,trend_score,confidence,is_risk_candidate,
                      risk_signals,secondary_flags,matched_keywords,classification_source,classification_reason,
                      harmfulness_score,taxonomy_fit_score,korea_relevance_score,concrete_context_score,
                      is_harmful,evidence_spans,
                      original_comment_count,kept_comment_count,duplicate_comments_removed,unrelated_comments_removed,
                      llm_model,llm_input_tokens,llm_output_tokens,llm_total_tokens,llm_estimated_cost_usd,
                      source,board_name,collection_type,extractor,published_at
               FROM content_records WHERE content_id=?""",
            (options[selected],),
        )[0]

        def _jl(v):   # JSON list 컬럼 안전 파싱
            try:
                return json.loads(v) if v else []
            except (json.JSONDecodeError, TypeError):
                return []

        st.markdown(f"### {detail['title'] or '(제목 없음)'}")
        if detail["taxonomy_lv2"]:
            st.markdown(f"🏷️ **{detail['taxonomy_lv1']} → {detail['taxonomy_lv2']} → {detail['category']}**  ·  "
                        f"{_ACTION_LABEL.get(detail['action'], detail['action'])}  ·  "
                        f"분류: `{detail['classification_source']}`")
            sc = st.columns(3)
            sc[0].metric("risk", detail["risk_score"])
            sc[1].metric("trend", detail["trend_score"])
            sc[2].metric("confidence", detail["confidence"])
            signals, secondary, matched = _jl(detail["risk_signals"]), _jl(detail["secondary_flags"]), _jl(detail["matched_keywords"])
            if signals:
                st.caption(f"위험신호: {', '.join(signals)}"
                           + (f"  ·  복합(secondary): {', '.join(secondary)}" if secondary else "")
                           + (f"  ·  매칭 키워드: {', '.join(matched)}" if matched else ""))
            if detail["classification_reason"]:
                st.caption(f"분류 근거: `{detail['classification_reason']}`")
            score_cols = st.columns(4)
            score_cols[0].metric("taxonomy fit", detail["taxonomy_fit_score"])
            score_cols[1].metric("harmfulness", detail["harmfulness_score"])
            score_cols[2].metric("Korea context", detail["korea_relevance_score"])
            score_cols[3].metric("concrete context", detail["concrete_context_score"])
            evidence = _jl(detail["evidence_spans"])
            if evidence:
                st.caption("판정 근거 문구: " + " · ".join(f"‘{x}’" for x in evidence))
        else:
            st.info(f"매핑 안 됨 · {_ACTION_LABEL.get(detail['action'], detail['action'])} "
                    f"(위험신호 후보={'예' if detail['is_risk_candidate'] else '아니오'})")
        st.caption(
            f"{detail['source'] or '?'} · {detail['board_name'] or ''} · {detail['extractor']} · "
            f"{detail['published_at'] or '날짜 없음'}  ·  [원문 열기]({detail['source_url']})"
        )
        with st.expander("본문 (masked)", expanded=True):
            st.write(detail["masked_text"] or "(본문 없음)")
        if detail["masked_comments"]:
            try:
                comments = json.loads(detail["masked_comments"])
            except (json.JSONDecodeError, TypeError):
                comments = [detail["masked_comments"]]
            with st.expander(f"댓글 {len(comments)}개 (masked)"):
                st.caption(
                    f"원본 {detail['original_comment_count']} · 유지 {detail['kept_comment_count']} · "
                    f"중복 제거 {detail['duplicate_comments_removed']} · 무관 제거 {detail['unrelated_comments_removed']}"
                )
                for cmt in comments:
                    st.markdown(f"- {cmt}")
        if detail["llm_total_tokens"]:
            st.caption(
                f"LLM `{detail['llm_model']}` · 입력 {detail['llm_input_tokens']:,} · "
                f"출력 {detail['llm_output_tokens']:,} · 합계 {detail['llm_total_tokens']:,} tokens · "
                f"추정 ${detail['llm_estimated_cost_usd']:.8f}"
            )
        if detail["filter_reason"]:
            st.caption(f"판정 사유: `{detail['filter_reason']}`")

        linked = query(
            db_path,
            """SELECT title,source_url,masked_text,link_source
               FROM content_records
               WHERE is_supplementary=1 AND parent_source_url=?
               ORDER BY rowid""",
            (detail["source_url"],),
        )
        linked_candidates = query(
            db_path,
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
                    st.write(item["masked_text"] or "(추출된 텍스트 없음)")
                for item in linked_candidates:
                    st.caption(
                        f"수집 실패/제외 · {item['link_source'] or 'body'} · "
                        f"`{item['status']}` · `{item['filter_reason'] or ''}` · {item['source_url']}"
                    )

with llm_tab:
    st.subheader("LLM 토큰 및 추정 비용")
    st.caption("API usage 기반이며, 설정된 모델 단가로 계산한 추정치입니다. 실제 청구액은 OpenAI 결제 내역이 기준입니다.")
    totals = query(db_path, """SELECT COALESCE(llm_model,'unknown') AS model,
        COUNT(*) AS calls, COALESCE(SUM(llm_input_tokens),0) AS input_tokens,
        COALESCE(SUM(llm_cached_input_tokens),0) AS cached_tokens,
        COALESCE(SUM(llm_output_tokens),0) AS output_tokens,
        COALESCE(SUM(llm_total_tokens),0) AS total_tokens,
        ROUND(COALESCE(SUM(llm_estimated_cost_usd),0),8) AS estimated_cost_usd
        FROM url_candidates WHERE llm_total_tokens>0 GROUP BY llm_model ORDER BY estimated_cost_usd DESC""")
    st.dataframe(totals, width="stretch", hide_index=True)
    st.subheader("저장 콘텐츠별 사용량")
    st.dataframe(query(db_path, """SELECT title,action,taxonomy_lv2,category,llm_model,
        llm_input_tokens,llm_cached_input_tokens,llm_output_tokens,llm_total_tokens,
        ROUND(llm_estimated_cost_usd,8) AS estimated_cost_usd,source_url
        FROM content_records WHERE llm_total_tokens>0 ORDER BY rowid DESC LIMIT 500"""),
        width="stretch", hide_index=True,
        column_config={"source_url": st.column_config.LinkColumn("URL", display_text="열기")})

with failures_tab:
    st.subheader("단계·사유별 실패")
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

with pii_tab:
    st.warning("raw_text와 원문 PII는 이 화면에서 조회하지 않습니다.")
    st.dataframe(query(
        db_path,
        """SELECT title,source_url,subtype,pii_types,ROUND(pii_risk_score,3) AS pii_risk,
                  masking_version,masking_warnings,masked_entities
           FROM content_records WHERE pii_detected=1
           ORDER BY pii_risk_score DESC LIMIT 300""",
    ), width="stretch", hide_index=True,
        column_config={"source_url": st.column_config.LinkColumn("URL")})
