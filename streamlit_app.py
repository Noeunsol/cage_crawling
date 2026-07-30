"""크롤러 SQLite를 읽기 전용으로 관찰하는 Streamlit 대시보드."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import streamlit as st

# 최종 판정: accepted/excluded/pending · 1차: keep/discard
_ACTION_LABEL = {
    "accepted": "✅ accepted", "excluded": "⛔ excluded", "pending": "⏳ pending",
    "keep": "📥 keep", "discard": "🗑️ discard",
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


db_path = st.sidebar.text_input("SQLite DB", "data/content.db")
if st.sidebar.button("새로고침"):
    st.cache_data.clear()

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
    st.metric("예상 최대 수집", f"{total_target:,}건")
    st.caption("실제 수집량은 사이트 가용 글 수에 따라 더 적을 수 있어요.")
    reset = st.checkbox("기존 DB 비우고 새로 수집")

    if st.checkbox("고급 설정"):
        trend_cfg = st.text_input("trend config", "configs/trend_collection.yaml")
        taxo_cfg = st.text_input("taxonomy", "configs/taxonomy.yaml")
    else:
        trend_cfg, taxo_cfg = "configs/trend_collection.yaml", "configs/taxonomy.yaml"

    overrides = {"target_by_source": {"dcinside": int(dc_target), "news_rss": int(news_target)},
                 "enabled": {"dcinside": use_dc, "news_rss": use_news}}

    b1, b2 = st.columns(2)
    if b1.button("미리보기"):
        with st.spinner("가용 규모 확인 중…"):
            try:
                from src import pipeline
                rep = pipeline.run_trend(trend_config=trend_cfg, taxonomy_config=taxo_cfg,
                                         db_path=db_path, dry_run=True, overrides=overrides)
                st.success(f"수집 가능 약 {rep.get('would_extract', 0):,}건")
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
                                         db_path=db_path, reset_db=reset,
                                         overrides=overrides, on_progress=_prog)
                bar.progress(1.0, text="완료")
                st.cache_data.clear()
                st.success(f"저장 {rep.get('stored_records', 0):,}건 · 아래 탭에서 확인")
                if rep.get("by_action"):
                    st.caption("처리 결과 (keep/review/mask/discard)")
                    st.json(rep["by_action"])
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

overview, prefilter_tab, candidates_tab, content_tab, failures_tab, pii_tab = st.tabs(
    ["Overview", "Pre-filter", "URL Candidates", "Content Explorer", "Failure Analysis", "PII"]
)

with overview:
    accepted_count = scalar(db_path, "SELECT COUNT(*) AS n FROM content_records WHERE action='accepted'")
    pending_count = scalar(db_path, "SELECT COUNT(*) AS n FROM content_records WHERE action='pending'")
    pii_count = scalar(db_path, "SELECT COUNT(*) AS n FROM content_records WHERE pii_detected=1")
    cols = st.columns(5)
    for col, label, value in zip(
        cols,
        ["URL 후보", "저장 콘텐츠", "✅ accepted", "⏳ pending", "PII 탐지"],
        [total_candidates, total_content, accepted_count, pending_count, pii_count],
    ):
        col.metric(label, value)

    st.subheader("Pipeline funnel")
    extracted = scalar(
        db_path,
        """SELECT COUNT(*) AS n FROM url_candidates
           WHERE status IN ('extracted','quality_failed','matched_pass','matched_review',
                            'matched_fail','duplicate','out_of_date_range',
                            'trend_accepted','trend_excluded','trend_pending','trend_discard')""",
    )
    accepted = scalar(
        db_path,
        "SELECT COUNT(*) AS n FROM url_candidates WHERE status IN ('matched_pass','trend_accepted','trend_pending')")
    st.bar_chart([
        {"stage": "discovered", "count": total_candidates},
        {"stage": "no-crawl", "count": scalar(
            db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status LIKE 'prefilter_%'")},
        {"stage": "extracted", "count": extracted},
        {"stage": "accepted/pending", "count": accepted},
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
    st.caption("RSS 제목·요약 또는 커뮤니티 제목·메타데이터만 판단했습니다. 본문과 댓글은 저장되지 않습니다.")
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
    statuses = ["전체"] + [row["status"] for row in query(
        db_path, "SELECT DISTINCT status FROM url_candidates WHERE status IS NOT NULL ORDER BY status")]
    methods = ["전체"] + [row["discovery_method"] for row in query(
        db_path,
        "SELECT DISTINCT discovery_method FROM url_candidates WHERE discovery_method IS NOT NULL ORDER BY discovery_method")]
    c1, c2 = st.columns(2)
    status = c1.selectbox("상태", statuses)
    method = c2.selectbox("Discovery method", methods)
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
                   filter_reason,parent_source_url,link_source
            FROM url_candidates {clause}
            ORDER BY rowid DESC LIMIT 500""",
        tuple(params),
    )
    st.dataframe(rows, width="stretch", hide_index=True,
                 column_config={"source_url": st.column_config.LinkColumn("URL")})

with content_tab:
    # ── 요약 지표 ──
    def _n(where=""):
        return scalar(db_path, f"SELECT COUNT(*) AS n FROM content_records {where}")
    # 저장되는 최종 상태는 accepted(분류 완료) + pending(LLM 대기). excluded/discard는 후보 로그에만 남음.
    mc = st.columns(4)
    mc[0].metric("전체 저장", _n("WHERE COALESCE(is_supplementary,0)=0"))
    mc[1].metric("✅ accepted", _n("WHERE action='accepted' AND COALESCE(is_supplementary,0)=0"))
    mc[2].metric("⏳ pending(LLM 대기)", _n("WHERE action='pending' AND COALESCE(is_supplementary,0)=0"))
    mc[3].metric("🔗 보조 콘텐츠", _n("WHERE is_supplementary=1"))

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
    action_sel = f3.selectbox("처리 상태", ["전체", "accepted", "pending"])
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
        f"""SELECT content_id,title,source_url,source,taxonomy_lv2,category,action,
                   risk_score,trend_score,confidence,ROUND(pii_risk_score,3) AS pii_risk
            FROM content_records {clause} ORDER BY collected_at DESC LIMIT 300""",
        tuple(params),
    )
    st.caption(f"{len(records)}건")
    table = [{"제목": r["title"] or "(제목 없음)", "수집원": r["source"], "URL": r["source_url"],
              "taxonomy": r["taxonomy_lv2"] or "—", "category": r["category"] or "—",
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
                      taxonomy_lv2,category,action,risk_score,trend_score,confidence,is_risk_candidate,
                      risk_signals,secondary_flags,matched_keywords,classification_source,classification_reason,
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
            st.markdown(f"🏷️ **{detail['taxonomy_lv2']} / {detail['category']}**  ·  "
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
                for cmt in comments:
                    st.markdown(f"- {cmt}")
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
            """SELECT source_url,status,filter_reason,link_source
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
