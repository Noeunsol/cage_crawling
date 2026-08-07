"""크롤러 SQLite를 읽기 전용으로 관찰하는 Streamlit 대시보드."""
from __future__ import annotations

import json
import importlib.util
import os
import sqlite3
import time
from pathlib import Path

import streamlit as st
import yaml
from dotenv import load_dotenv

# 로컬 운영 UI에서는 프로젝트 .env가 정본이다. 이미 떠 있는 셸의 오래된 키보다 우선한다.
_ENV_PATH = Path(__file__).with_name(".env")
load_dotenv(_ENV_PATH, override=True)

from src.logging_setup import setup_logging  # noqa: E402
from src.pipelines.stages import _build_matcher, _load_llm_cfg  # noqa: E402  (crawler_settings + configs/llm.yaml 병합)
from src.classify.matcher import build_taxonomy_index  # noqa: E402
from src.policy import load_policies  # noqa: E402
from src.pipelines.taxonomy_adjudication import _trend_classification_action  # noqa: E402
setup_logging(component="streamlit")

def _fmt_dur(sec: float) -> str:
    """소요 시간 사람이 읽기 좋게. 60s 미만은 초, 이상은 m s."""
    sec = round(sec)
    return f"{sec}s" if sec < 60 else f"{sec // 60}m {sec % 60}s"


# 최종 판정: accepted/discard · 1차: keep/discard
_ACTION_LABEL = {
    "accepted": "✅ accepted", "discard": "🗑️ discard",
    "keep": "📥 keep", "discard": "🗑️ discard",
}
_STATUS_LABEL = {
    "prefilter_discarded": "제목 단계 제외",
    "sampling_skipped": "날짜·시간 표본 미선택",
    "extraction_failed": "본문 수집 실패",
    "trend_discard": "본문 확인 후 제외",
    "trend_excluded": "이전 버전 LLM 제외",
    "trend_accepted": "최종 채택",
    "trend_review": "이전 버전 검토 상태",
    "trend_pending": "이전 버전 분류 대기",
    "duplicate": "중복 콘텐츠",
    "supplementary_collected": "보조 링크 수집 완료",
    "discovered": "발견",
}
_METHOD_LABEL = {
    "board_list": "게시판 목록",
    "rss": "뉴스 RSS",
    "in_body_link": "본문·댓글 내부 링크",
}

st.set_page_config(page_title="CAGE 콘텐츠 수집", page_icon="🕸️", layout="wide")
st.title("CAGE 콘텐츠 수집")
st.caption("1차 트렌드 탐색 → taxonomy 커버리지 확인 → 2차 부족분 보강 · raw 원문은 표시하지 않습니다.")


@st.cache_data(ttl=5)
def query(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(db_path: str, sql: str, params: tuple = ()) -> int:
    rows = query(db_path, sql, params)
    return next(iter(rows[0].values())) if rows else 0


def _as_list(value):
    if isinstance(value, list):
        return value
    if not value:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def dependency_status(module: str, env_name: str, enabled: bool = True) -> tuple[bool, str]:
    """외부 연동의 로컬 실행 조건을 검사한다. 실제 API 인증은 호출 시 확정된다."""
    if not enabled:
        return False, "config에서 비활성화됨"
    missing = []
    if not os.getenv(env_name):
        missing.append(f"{env_name} 없음")
    if importlib.util.find_spec(module) is None:
        missing.append(f"{module} SDK 없음")
    return not missing, " · ".join(missing) if missing else "키·SDK 확인 완료"


def discovery_cache_key(intent, rerank_cfg: dict) -> str:
    """intent나 rerank 설정이 바뀌면 이전 Tavily 결과를 재사용하지 않는다."""
    payload = {"intent": vars(intent), "rerank": rerank_cfg}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _collection_rows(stats: dict, preview: bool = False) -> list[dict]:
    """최근 N일 source별 수집 통계를 UI 표로 바꾼다."""
    return [
        {
            "수집원": source,
            "기간 내 후보": values.get("available", 0),
            "제목 통과": values.get("title_keep", 0),
            "본문 표본": values.get("selected", values.get("title_keep", 0)),
            **({} if preview else {
                "후보 처리": values.get("scanned", 0),
                "기존 처리 건너뜀": values.get("already_processed", 0),
                "accepted": values.get("accepted", 0),
                "제외·실패": sum(values.get(k, 0) for k in (
                "prefilter_discard", "content_discard", "extraction_failed",
                "discard", "duplicate",
                )),
            }),
            "안전 상한 도달": "예" if values.get("cap_reached") else "아니오",
        }
        for source, values in stats.items()
    ]


st.sidebar.header("실행 설정")
# P1: 2차 실행이 저장한 DB로 결과 뷰어를 자동 전환 (위젯 생성 전에만 세션값 갱신 가능)
if "_pending_result_db" in st.session_state:
    st.session_state["result_db"] = st.session_state.pop("_pending_result_db")
st.session_state.setdefault("result_db", "data/db/content.db")
db_path = st.sidebar.text_input("결과 DB", key="result_db", help="1차 결과와 taxonomy 현황을 읽는 DB입니다. 2차 실행 후 저장 DB로 자동 전환됩니다.")
trend_cfg = st.sidebar.text_input("1차 수집 config", "configs/trend_collection.yaml")
taxo_cfg = st.sidebar.text_input("Taxonomy config", "configs/taxonomy.yaml")
settings_cfg = st.sidebar.text_input("공통 crawler config", "configs/crawler_settings.yaml")
p2_config = st.sidebar.text_input("2차 수집 config", "configs/phase2_semantic_collection.yaml")
refresh_col, env_col = st.sidebar.columns(2)
if refresh_col.button("새로고침"):
    st.cache_data.clear()
if env_col.button(".env 다시 읽기"):
    load_dotenv(_ENV_PATH, override=True)
    st.cache_data.clear()
    st.rerun()

with st.sidebar.expander("1단계 · 트렌드 수집", expanded=not Path(db_path).is_file()):
    st.markdown("**수집 기간 · 수집원**")
    use_dc = st.checkbox("디시인사이드", value=True)
    use_news = st.checkbox("뉴스 RSS", value=True)
    st.caption("FM코리아·네이트판은 다음 단계 지원 예정")
    lookback_days = st.selectbox("최근 며칠", [1, 3, 5], index=0)
    st.metric("수집 범위", f"최근 {int(lookback_days)}일")
    dc_daily_cap = st.number_input(
        "디시 1일 최대 수집",
        min_value=0,
        max_value=1000,
        value=200,
        step=50,
        help="하루에 디시인사이드에서 본문 수집할 최대 건수입니다. 0이면 수집하지 않습니다.",
    )
    news_daily_cap = st.number_input(
        "뉴스 RSS 1일 최대 수집",
        min_value=0,
        max_value=1000,
        value=100,
        step=25,
        help="하루에 뉴스 RSS에서 본문 수집할 최대 건수입니다. 0이면 수집하지 않습니다.",
    )
    source_daily = (dc_daily_cap if use_dc else 0) + (news_daily_cap if use_news else 0)
    st.caption(
        f"본문 후보 최대 {source_daily * int(lookback_days):,}건 "
        f"(디시 날짜당 {int(dc_daily_cap)} · 뉴스 날짜당 {int(news_daily_cap)}, 4시간대 균등 표본)"
    )
    st.caption("해당 기간에 게시된 가용 후보를 수집하고, 제목 필터와 LLM을 거쳐 taxonomy를 매핑합니다.")
    st.caption("본문 키워드 룰로 재탈락시키지 않으며, OCR 없이 제목과 HTML 본문을 LLM이 최종 분류합니다.")
    reset = st.checkbox("기존 DB 비우고 새로 수집")

    try:
        settings_data = yaml.safe_load(Path(settings_cfg).read_text(encoding="utf-8"))
    except Exception:
        settings_data = {}
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

    with st.expander("LLM 분류 상태"):
        try:
            llm_cfg = settings_data.get("matching", {})
            llm_eff = _load_llm_cfg(settings_data)   # configs/llm.yaml + settings.matching.llm 병합
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
                f"accepted: confidence ≥ {llm_cfg.get('accepted_confidence', 0.75)}, "
                f"taxonomy fit ≥ {llm_cfg.get('accepted_taxonomy_fit', 0.50)}, "
                f"concrete context ≥ {llm_cfg.get('accepted_concrete_context', 0.30)}, "
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
            "daily_quota_by_source": {
                "dcinside": int(dc_daily_cap),
                "news_rss": int(news_daily_cap),
            }
        },
        "enabled": {"dcinside": use_dc, "news_rss": use_news},
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
                    st.dataframe(_collection_rows(rep["collection_targets"], preview=True), hide_index=True)
                st.json(rep.get("by_source", {}))
            except Exception as exc:  # noqa: BLE001 (UI 표시)
                st.error(f"실패: {exc}")

    if b2.button("1차 수집 시작", type="primary"):
        if not (use_dc or use_news):
            st.warning("수집원을 하나 이상 선택하세요.")
        else:
            bar = st.progress(0.0, text="목록 수집 중…")

            def _prog(done, total):
                bar.progress(done / total if total else 1.0, text=f"{done}/{total}건 처리 중…")

            try:
                from src import pipeline
                _t0 = time.perf_counter()
                rep = pipeline.run_trend(trend_config=trend_cfg, taxonomy_config=taxo_cfg,
                                         settings_config=settings_cfg, db_path=db_path, reset_db=reset,
                                         overrides=overrides, on_progress=_prog)
                _elapsed = time.perf_counter() - _t0
                bar.progress(1.0, text="완료")
                st.cache_data.clear()
                st.session_state["last_trend_run"] = {
                    "report": rep,
                    "elapsed": _elapsed,
                    "lookback_days": int(lookback_days),
                    "db_path": db_path,
                }
                st.success(f"저장 {rep.get('stored_records', 0):,}건 · 소요 {_fmt_dur(_elapsed)} · 아래 탭에서 확인")
                if rep.get("by_action"):
                    st.caption("최종 분류 결과 (accepted/discard)")
                    st.json(rep["by_action"])
                if rep.get("collection_targets"):
                    st.caption(f"최근 {int(lookback_days)}일 수집 결과")
                    st.dataframe(_collection_rows(rep["collection_targets"]), hide_index=True)
                window_result = rep.get("collection_window", {})
                if window_result:
                    st.info(
                        f"최근 {window_result.get('lookback_days', lookback_days)}일 · "
                        f"본문 표본 {window_result.get('selected', 0)} · "
                        f"accepted {window_result.get('accepted', 0)}"
                    )
            except Exception as exc:  # noqa: BLE001 (UI 표시)
                st.error(f"실행 실패: {exc}")

if not Path(db_path).is_file():
    st.info(f"`{db_path}`가 아직 없습니다. 사이드바 **'▶ 트렌드 수집 실행'** 에서 시작하거나 CLI로 크롤러를 돌린 뒤 새로고침하세요.")
    st.stop()

# 구 스키마 DB를 현재 컬럼으로 맞춘다(비파괴 ADD COLUMN). 신규 컬럼(risk_signals 등) 조회 가능하게.
try:
    from src.storage.store import Store
    Store(db_path).close()
except Exception as exc:  # noqa: BLE001 (마이그레이션 실패해도 조회는 시도)
    st.warning(f"스키마 자동 정렬 건너뜀: {exc}")

try:
    total_candidates = scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates")
    total_content = scalar(db_path, "SELECT COUNT(*) AS n FROM content_records")
except sqlite3.Error as exc:
    st.error(f"DB를 읽을 수 없습니다: {exc}")
    st.stop()

taxonomy_tab, phase2_tab, recent_tab, overview, content_tab, candidates_tab, llm_tab, failures_tab, prefilter_tab, pii_tab = st.tabs(
    ["Taxonomy 현황", "Tavily 2차 수집", "방금 실행", "전체 요약", "콘텐츠 탐색", "URL 후보", "토큰·비용", "실패 분석", "사전 필터", "PII"]
)

with recent_tab:
    st.subheader("방금 실행한 1차 수집")
    last_run = st.session_state.get("last_trend_run")
    if last_run and last_run.get("report"):
        rep = last_run["report"]
        st.caption(
            f"최근 {last_run.get('lookback_days', 1)}일 · "
            f"소요 {_fmt_dur(last_run.get('elapsed', 0.0))} · DB=`{last_run.get('db_path', db_path)}`"
        )
        st.metric("저장", f"{rep.get('stored_records', 0):,}")
        st.metric("accepted", f"{rep.get('by_action', {}).get('accepted', 0):,}")
        st.metric("discard", f"{rep.get('by_action', {}).get('discard', 0):,}")
        if rep.get("collection_targets"):
            st.dataframe(_collection_rows(rep["collection_targets"]), hide_index=True, width="stretch")
        if rep.get("filter_fail_reasons"):
            st.subheader("주요 실패 사유")
            st.dataframe(
                [{"reason": k, "count": v} for k, v in rep.get("filter_fail_reasons", {}).items()],
                hide_index=True, width="stretch",
            )
    else:
        st.info("아직 방금 실행한 1차 수집 결과가 없습니다. 위에서 1차 수집을 한 번 실행해 보세요.")

with taxonomy_tab:
    st.subheader("Taxonomy별 콘텐츠 현황")
    st.caption("accepted만 유효 커버리지로 계산하며, 2차 수집은 부족분이 큰 LV2부터 보강합니다.")
    taxonomy_rows = query(db_path, """
        SELECT taxonomy_lv1, taxonomy_lv2,
               SUM(CASE WHEN action='accepted' THEN 1 ELSE 0 END) AS accepted,
               SUM(CASE WHEN action='accepted' THEN 1 ELSE 0 END) AS effective,
               SUM(CASE WHEN collection_phase=2 THEN 1 ELSE 0 END) AS phase2,
               COALESCE(SUM(llm_total_tokens),0) AS tokens,
               ROUND(COALESCE(SUM(llm_estimated_cost_usd),0),6) AS cost_usd
        FROM content_records
        WHERE COALESCE(is_supplementary,0)=0 AND taxonomy_lv2 IS NOT NULL AND taxonomy_lv2!=''
        GROUP BY taxonomy_lv1,taxonomy_lv2 ORDER BY effective ASC, taxonomy_lv2
    """)
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
                                      "accepted": 0, "effective": 0.0,
                                      "phase2": 0, "tokens": 0, "cost_usd": 0.0})
        for row in taxonomy_rows:
            row["target"] = float(targets.get(row["taxonomy_lv2"], default_target))
            row["shortfall"] = max(0.0, row["target"] - float(row["effective"] or 0))
        taxonomy_rows.sort(key=lambda row: (-row["shortfall"], row["taxonomy_lv2"]))
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Taxonomy 목표 설정을 읽지 못했습니다: {exc}")
    if taxonomy_rows:
        chosen_taxonomy = st.selectbox(
            "Taxonomy 선택", ["전체"] + [r["taxonomy_lv2"] for r in taxonomy_rows],
            help="선택하면 아래에서 해당 taxonomy의 상태·수집 단계·콘텐츠를 한 번에 확인합니다.",
        )
        st.dataframe(taxonomy_rows, width="stretch", hide_index=True,
                     column_config={"cost_usd": st.column_config.NumberColumn("추정 비용($)", format="$%.6f")})

        with st.expander("기존 DB 재검토 모의실험", expanded=False):
            st.caption("현재 taxonomy 기준으로 기존 DB 샘플을 다시 판정만 해보고, 저장은 하지 않습니다.")
            recheck_n = st.number_input("검사 수량", min_value=1, max_value=500, value=20, step=5)
            recheck_only_accepted = st.checkbox("기존 accepted만 검사", value=False)
            if st.button("모의 실험 실행"):
                try:
                    settings_data = yaml.safe_load(Path(settings_cfg).read_text(encoding="utf-8")) or {}
                    matcher = _build_matcher(settings_data, 0.8, 0.5)
                    policies = load_policies(taxo_cfg)
                    valid_pairs, taxo_lines = build_taxonomy_index(policies)
                    where = "WHERE action='accepted'" if recheck_only_accepted else "WHERE action IN ('accepted','discard')"
                    rows = query(db_path, f"SELECT * FROM content_records {where} ORDER BY RANDOM() LIMIT ?", (int(recheck_n),))
                    if not rows:
                        st.info("검사할 레코드가 없습니다.")
                    else:
                        changed = []
                        summary = {"accepted": 0, "discard": 0, "unchanged": 0}
                        for row in rows:
                            rec = None
                            try:
                                row["masked_comments"] = _as_list(row.get("masked_comments"))
                                row["raw_comments"] = _as_list(row.get("raw_comments"))
                                row["masked_entities"] = _as_list(row.get("masked_entities"))
                                row["pii_types"] = _as_list(row.get("pii_types"))
                                row["risk_signals"] = _as_list(row.get("risk_signals"))
                                row["matched_keywords"] = _as_list(row.get("matched_keywords"))
                                row["secondary_flags"] = _as_list(row.get("secondary_flags"))
                                row["evidence_spans"] = _as_list(row.get("evidence_spans"))
                                row["negative_contexts"] = _as_list(row.get("negative_contexts"))
                                row["masking_warnings"] = _as_list(row.get("masking_warnings"))
                                rec = ContentRecord(**row)
                                match = matcher.llm.classify(rec, policies, valid_pairs, taxo_lines) if matcher.llm else matcher.rule.classify(rec, policies)
                                if match is None:
                                    new_action = "discard"
                                    reason = "llm_failed"
                                else:
                                    new_action = "accepted" if _trend_classification_action(match, rec, settings_data) == "accepted" else "discard"
                                    reason = match.reason
                                old_action = row.get("action") or row.get("filter_status") or ""
                                if new_action == old_action:
                                    summary["unchanged"] += 1
                                else:
                                    summary[new_action] += 1
                                    changed.append({
                                        "title": row.get("title", ""),
                                        "old": old_action,
                                        "new": new_action,
                                        "taxonomy_lv2": row.get("taxonomy_lv2") or "",
                                        "reason": reason,
                                    })
                            except Exception as exc:  # noqa: BLE001
                                changed.append({"title": row.get("title", ""), "old": "", "new": "error", "taxonomy_lv2": "", "reason": str(exc)})
                        st.write(summary)
                        if changed:
                            st.dataframe(changed, hide_index=True, width="stretch")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"모의 실험 실패: {exc}")

        if chosen_taxonomy != "전체":
            phase_rows = query(db_path, """
                SELECT CASE WHEN collection_phase=2 THEN '2차 보강' ELSE '1차 수집' END AS phase,
                       action,COUNT(*) AS content_count,COALESCE(SUM(llm_total_tokens),0) AS tokens,
                       ROUND(COALESCE(SUM(llm_estimated_cost_usd),0),6) AS cost_usd
                FROM content_records WHERE taxonomy_lv2=? AND COALESCE(is_supplementary,0)=0
                GROUP BY phase,action ORDER BY phase,action
            """, (chosen_taxonomy,))
            st.dataframe(phase_rows, width="stretch", hide_index=True)
            st.dataframe(query(db_path, """
                SELECT title,action,category,CASE WHEN collection_phase=2 THEN '2차' ELSE '1차' END AS phase,
                       ROUND(taxonomy_fit_score,2) AS taxonomy_fit,
                       ROUND(korea_relevance_score,2) AS korea_relevance,source_url
                FROM content_records WHERE taxonomy_lv2=? AND COALESCE(is_supplementary,0)=0
                ORDER BY action,rowid DESC LIMIT 200
            """, (chosen_taxonomy,)), width="stretch", hide_index=True,
                column_config={"source_url": st.column_config.LinkColumn("URL", display_text="열기")})
    else:
        st.info("분류된 콘텐츠가 없습니다. 먼저 1차 수집을 실행하세요.")

with phase2_tab:
    # ── 2차 실행 컨트롤 (①intent → ②discovery → ③보강). 결과는 이 아래 뷰어에서 바로 확인.
    st.subheader("2단계 · 부족한 안전 카테고리 채우기")
    st.markdown(
        "1차 수집만으로 **목표치보다 모자란 안전 카테고리**를, 웹 검색(Tavily)으로 관련 글을 찾아 채우는 단계입니다.  \n"
        "**순서 ① 검색문 확인 → ② 검색결과 미리보기 → ③ 실제 수집·저장.** "
        "①②는 확인용이라 건너뛰고 ③만 눌러도 됩니다. 처음이면 아래에서 카테고리 1~2개만 고르고 **③**을 눌러 보세요."
    )
    _kb = st.columns(2)
    _tavily_ready, _tavily_reason = dependency_status("tavily", "TAVILY_API_KEY")
    _openai_enabled = bool(_load_llm_cfg(settings_data).get("enabled", False))
    _openai_ready, _openai_reason = dependency_status("openai", "OPENAI_API_KEY", _openai_enabled)
    if _tavily_ready:
        _kb[0].success(f"TAVILY 로컬 준비 완료 · {_tavily_reason}")
    else:
        _kb[0].error(f"TAVILY 준비 안 됨 · {_tavily_reason} → Mock provider 사용")
    if _openai_ready:
        _kb[1].success(f"OPENAI 로컬 준비 완료 · {_openai_reason}")
    else:
        _kb[1].warning(f"OPENAI 준비 안 됨 · {_openai_reason} → classify 결과 discard")
    st.caption("로컬 준비 완료는 config·환경변수·SDK 검사 결과입니다. API 키의 유효성·권한·잔액은 실제 Preview 호출에서 확정됩니다.")

    with st.expander("고급 설정 (기본값 그대로 둬도 됩니다)"):
        st.caption("점수는 0~1. 높일수록 엄격(정확·소량), 낮출수록 관대(다량·잡음↑).")
        rc = st.columns(3)
        md = rc[0].slider("후보 관련성 최소점수", 0.0, 1.0, 0.65, 0.05,
                          help="검색 후보가 주제와 관련될 최소 점수. 미만이면 본문을 안 가져옵니다. 높이면 더 엄격.")
        mk = rc[1].slider("후보 한국관련성 최소점수", 0.0, 1.0, 0.60, 0.05,
                          help="한국 콘텐츠일 가능성 기준. 높이면 비한국 후보를 더 걸러냄.")
        lp = rc[2].slider("저우선 후보 기준", 0.0, 1.0, 0.45, 0.05,
                          help="관련성이 이 값 이상~기준 미만이면 '저우선'으로 분류. 보통 그대로 둡니다.")
        ac = st.columns(3)
        af = ac[0].slider("저장승인·주제 적합도", 0.0, 1.0, 0.75, 0.05,
                          help="본문 분석 후 accepted로 저장할 최소 적합도. 높이면 확실한 것만 저장.")
        ak = ac[1].slider("저장승인·한국 관련성", 0.0, 1.0, 0.60, 0.05,
                          help="본문 기준 한국 관련성. 미달이면 저장하지 않습니다.")
        an = ac[2].slider("저장승인·구체성", 0.0, 1.0, 0.60, 0.05,
                          help="추상적 설명이 아니라 실제 사례·피해가 담긴 글만. 높이면 더 구체적인 것만.")
        lc = st.columns(3)
        p2_max_lv2 = lc[0].number_input("한 번에 처리할 카테고리 수", 1, 19, 5,
                                        help="많이 선택해도 이 수만큼만 처리합니다.")
        p2_max_fetch = lc[1].number_input("카테고리당 본문 수집 상한", 1, 100, 20)
        p2_max_classify = lc[2].number_input("LLM 분류 총 상한(비용 상한)", 1, 500, 60,
                                             help="이 횟수를 넘으면 분류를 멈춥니다. 비용 사고 방지.")
        p2_target = st.number_input("카테고리별 목표 건수", 1, 1000, 30,
                                    help="accepted로 확정된 콘텐츠 수를 기준으로 부족분을 계산합니다.")
    p2_overrides = {
        "rerank": {"min_discovery_relevance": md, "min_korea_relevance": mk, "low_priority_relevance": lp},
        "acceptance": {"min_taxonomy_fit_score": af, "min_korea_relevance_score": ak,
                       "min_concrete_context_score": an},
        "limits": {"max_selected_lv2": int(p2_max_lv2), "max_fetch_per_lv2": int(p2_max_fetch),
                   "max_total_llm_classify": int(p2_max_classify)},
        "target_selection": {"min_accepted_per_lv2": int(p2_target), "review_weight": 0.0},
    }

    try:
        from src import pipeline as _p2
        p2_intents = _p2.preview_intents(p2_config, db_path, taxonomy_config=taxo_cfg,
                                         overrides=p2_overrides)
    except Exception as exc:  # noqa: BLE001 (UI 표시)
        st.error(f"intent 로드 실패: {exc}")
        p2_intents = []
    p2_by_lv2 = {it["lv2"]: it for it in p2_intents}
    try:
        _pol_meta = {p.taxonomy_lv2: (p.taxonomy_lv2_name or p.taxonomy_lv2, p.description or "")
                     for p in load_policies(taxo_cfg)}
    except Exception:  # noqa: BLE001
        _pol_meta = {}

    def _lv2_label(code: str) -> str:
        name = _pol_meta.get(code, (code, ""))[0]
        d = p2_by_lv2.get(code, {}).get("deficit")
        return f"{name} · 부족 {d:g}건" if d is not None else name

    p2_selected = st.multiselect(
        "보강할 안전 카테고리 (부족분이 큰 순서)", list(p2_by_lv2),
        default=list(p2_by_lv2)[:2], format_func=_lv2_label, key="p2_sel",
        help="1차 수집이 목표치보다 모자란 카테고리입니다. 처음이면 1~2개만 골라 보세요.")
    for _c in p2_selected:
        _desc = _pol_meta.get(_c, ("", ""))[1]
        if _desc:
            st.caption(f"• **{_pol_meta[_c][0]}** — {_desc}")

    wc = st.columns([2, 1, 1])
    p2_scratch = wc[0].text_input("Small Run 저장 DB", "data/db/phase2_pilot.db",
                                  help="기본은 scratch DB. 튜닝 중 content.db를 더럽히지 않는다.")
    p2_use_main = wc[1].checkbox("content.db에 저장", value=False)
    p2_limit = wc[2].number_input("실제 본문 fetch 상한", 1, 60, 6,
                                  help="Tavily API 호출 횟수가 아니라, 검색 후 실제 URL 본문을 가져올 최대 건수입니다.")
    p2_write_db = db_path if p2_use_main else p2_scratch
    st.caption("⚠️ Discovery Preview와 2차 보강 실행은 Tavily 검색을 각각 새로 호출합니다. "
               "현재 advanced 검색은 선택 LV2당 호출 1회·2 credits이며, fetch 상한과는 별도입니다.")
    if p2_use_main:
        st.warning("실제 content.db에 저장합니다. Tavily/OpenAI 유료 호출이 발생할 수 있습니다.")

    _cache = st.session_state.get("p2_discovery_cache", {})
    _cached_sel = [lv2 for lv2 in p2_selected
                   if (lv2, discovery_cache_key(p2_by_lv2[lv2]["intent"], p2_overrides["rerank"])) in _cache] if p2_selected else []
    _n = len(p2_selected)

    st.markdown("#### 실행 순서 — 위에서 아래로")
    if not p2_selected:
        st.info("먼저 위에서 **보강할 카테고리**를 1개 이상 고르세요. 선택하면 아래 2·3단계가 열립니다.")

    # 1단계 (선택) — 검색문만 확인, 비용 없음
    with st.container(border=True):
        st.markdown("**1단계 · 검색문 미리보기**　`선택`　`비용 없음`")
        st.caption("각 카테고리를 어떤 문장으로 검색할지 미리 확인합니다. 건너뛰어도 됩니다.")
        _go1 = st.button("검색문 보기", key="p2_b1", width="stretch")
    if _go1:
        from src.phase2.intent_builder import validate_collection_intent
        intent_rows = []
        for it in p2_intents:
            check = validate_collection_intent(it["intent"])
            intent_rows.append({"LV2": it["lv2"], "target": it["target"], "effective": it["effective"],
                                "deficit": it["deficit"], "intent": "수동" if it["intent"].is_manual else "자동",
                                "검증 점수": check["score"], "상태": check["status"],
                                "경고": " · ".join(check["warnings"]) or "없음"})
        st.dataframe(intent_rows, hide_index=True, width="stretch")
        for lv2 in p2_selected:
            it = p2_by_lv2.get(lv2)
            if it:
                intent = it["intent"]
                check = validate_collection_intent(intent)
                st.markdown(f"**{lv2}** · 자연어 intent · `{check['status']} {check['score']}/100`")
                st.write(intent.natural_language_query)
                st.caption(f"include: {intent.include}  ·  exclude: {intent.exclude}"
                           + ("  ·  ⚠️ sensitive(auto-discard)" if intent.force_review else ""))
                if check["warnings"]:
                    st.warning(" · ".join(check["warnings"]))

    # 2단계 (선택) — 실제 검색으로 후보 확인, 소량 비용
    with st.container(border=True):
        _s2 = f"미리보기 캐시 {len(_cached_sel)}/{_n}개 준비됨" if p2_selected else "카테고리 선택 대기"
        st.markdown("**2단계 · 검색 미리보기**　`선택`　`소량 비용`")
        st.caption(f"Tavily로 실제 검색해 후보 URL·점수를 확인합니다. {_s2}.")
        _go2 = st.button("검색 미리보기 실행", key="p2_b2", width="stretch", disabled=not p2_selected)
    if _go2:
        if not p2_selected:
            st.warning("카테고리를 하나 이상 선택하세요.")
        else:
            provider = _p2._default_provider()
            for lv2 in p2_selected:
                it = p2_by_lv2.get(lv2)
                if not it:
                    continue
                with st.spinner(f"{lv2} discovery…"):
                    try:
                        entries = _p2.preview_discovery(it["intent"], provider, None, p2_overrides["rerank"])
                        cache = st.session_state.setdefault("p2_discovery_cache", {})
                        cache[(lv2, discovery_cache_key(it["intent"], p2_overrides["rerank"]))] = entries
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"{lv2} discovery 실패: {exc}")
                        continue
                st.markdown(f"**{lv2}** · {len(entries)}건 (fetch={sum(e['rerank'].fetch_decision=='fetch' for e in entries)})")
                if entries:
                    fetch_count = sum(e["rerank"].fetch_decision == "fetch" for e in entries)
                    skip_count = sum(e["rerank"].fetch_decision == "skip" for e in entries)
                    avg_discovery = sum(e["rerank"].discovery_relevance_score for e in entries) / len(entries)
                    avg_korea = sum(e["rerank"].korea_relevance_score for e in entries) / len(entries)
                    qc = st.columns(4)
                    qc[0].metric("후보 적중 proxy", f"{fetch_count / len(entries):.0%}", help="fetch 판정 후보 비율이며 최종 taxonomy 정확도는 아닙니다.")
                    qc[1].metric("평균 discovery", f"{avg_discovery:.2f}")
                    qc[2].metric("평균 Korea", f"{avg_korea:.2f}")
                    qc[3].metric("skip", skip_count)
                st.dataframe([{
                    "title": e["result"].title, "url": e["result"].url,
                    "provider": round(e["result"].provider_score or 0, 2),
                    "discovery": e["rerank"].discovery_relevance_score,
                    "korea": e["rerank"].korea_relevance_score,
                    "decision": e["rerank"].fetch_decision, "by": e["rerank"].source,
                } for e in entries], hide_index=True, width="stretch",
                    column_config={"url": st.column_config.LinkColumn("url", display_text="열기")})

    # 3단계 (필수) — 실제 본문 수집·분류·저장, 비용 발생
    with st.container(border=True):
        _will = (_n - len(_cached_sel)) if p2_selected else 0
        st.markdown("**3단계 · 수집 · 저장**　`필수`　`비용 발생`")
        st.caption(f"후보 본문을 가져와 분류·저장합니다. 신규 웹검색 {_will}회 예정 · 저장 위치 `{p2_write_db}`.")
        _go3 = st.button("수집 · 저장 실행", key="p2_b3", type="primary", width="stretch", disabled=not p2_selected)
    if _go3:
        if not p2_selected:
            st.warning("카테고리를 하나 이상 선택하세요.")
        else:
            bar = st.progress(0.0, text="실행 중…")
            try:
                session_cache = st.session_state.get("p2_discovery_cache", {})
                cached_discovery = {
                    lv2: session_cache[(lv2, discovery_cache_key(p2_by_lv2[lv2]["intent"], p2_overrides["rerank"]))]
                    for lv2 in p2_selected
                    if (lv2, discovery_cache_key(p2_by_lv2[lv2]["intent"], p2_overrides["rerank"])) in session_cache
                }
                if cached_discovery:
                    suffix = "Tavily 재호출 없음" if len(cached_discovery) == len(p2_selected) else "나머지 LV2만 Tavily 호출"
                    st.info(f"Discovery Preview 캐시 재사용: {len(cached_discovery)}/{len(p2_selected)}개 LV2 · {suffix}")
                _t0 = time.perf_counter()
                rep = _p2.small_run(
                    p2_selected, int(p2_limit), p2_config, p2_write_db,
                    taxonomy_config=taxo_cfg, settings_config=settings_cfg,
                    overrides=p2_overrides,
                    discovery_cache=cached_discovery,
                    on_progress=lambda d, t: bar.progress(min(d / t, 1.0) if t else 1.0, text=f"{d}/{t}"))
                _elapsed = time.perf_counter() - _t0
                bar.progress(1.0, text="완료")
                st.cache_data.clear()
                st.success(f"저장 {rep.get('stored_records', 0)}건 · 소요 {_fmt_dur(_elapsed)} · DB=`{p2_write_db}` · run_id=`{rep.get('run_id','')}`")
                if p2_write_db != db_path:
                    st.session_state["_pending_result_db"] = p2_write_db
                    st.caption(f"다음 새로고침부터 사이드바 '결과 DB'가 `{p2_write_db}`로 전환되어 아래 뷰어에서 이 실행을 볼 수 있습니다.")
                st.subheader("이번 실행 저장 결과")
                st.dataframe(query(p2_write_db, """
                    SELECT title,taxonomy_lv2_candidate AS target_taxonomy,taxonomy_lv2 AS predicted_taxonomy,
                           action,filter_reason,llm_total_tokens,
                           ROUND(llm_estimated_cost_usd,6) AS llm_cost_usd,source_url
                    FROM content_records WHERE run_id=? ORDER BY rowid DESC
                """, (rep.get("run_id", ""),)), width="stretch", hide_index=True,
                    column_config={"source_url": st.column_config.LinkColumn("URL", display_text="열기")})
                st.subheader("Coverage (before → after)")
                st.dataframe([{"LV2": k, **v} for k, v in rep.get("coverage", {}).items()],
                             hide_index=True, width="stretch")
                st.subheader("Provider 성과")
                st.dataframe([{"provider": k, **v} for k, v in rep.get("provider_performance", {}).items()],
                             hide_index=True, width="stretch")
            except Exception as exc:  # noqa: BLE001
                st.error(f"실행 실패: {exc}")

    st.divider()
    st.subheader("Tavily 2차 수집 결과 (저장 이력)")
    st.caption("Tavily가 발견한 URL 후보와 실제 페이지에서 추출·분류해 저장한 콘텐츠를 run 단위로 확인합니다. Tavily snippet은 후보 판단용이며 저장 본문과 구분됩니다.")
    phase2_runs = query(db_path, """
        SELECT run_id,MAX(rowid) AS latest_row,COUNT(*) AS candidates,
               SUM(CASE WHEN status='rerank_skipped' THEN 1 ELSE 0 END) AS rerank_skipped,
               SUM(CASE WHEN status='extraction_failed' THEN 1 ELSE 0 END) AS extraction_failed,
               SUM(CASE WHEN status='trend_accepted' THEN 1 ELSE 0 END) AS accepted,
               SUM(CASE WHEN status='trend_discard' THEN 1 ELSE 0 END) AS discarded,
               COALESCE(SUM(llm_total_tokens),0) AS tokens,
               ROUND(COALESCE(SUM(llm_estimated_cost_usd),0),6) AS llm_cost_usd
        FROM url_candidates
        WHERE collection_phase=2 AND run_id IS NOT NULL AND run_id!=''
        GROUP BY run_id ORDER BY latest_row DESC
    """)
    if not phase2_runs:
        st.info("현재 결과 DB에 Tavily 2차 실행 이력이 없습니다. scratch DB로 실행했다면 사이드바의 '결과 DB'를 해당 파일로 바꾸세요.")
    else:
        run_options = [row["run_id"] for row in phase2_runs]
        run_id = st.selectbox("실행 run_id", run_options, key="phase2_run")
        run_summary = next(row for row in phase2_runs if row["run_id"] == run_id)
        rm = st.columns(6)
        for col, label, value in zip(
            rm, ["발견 후보", "rerank 제외", "추출 실패", "accepted", "discard", "LLM 추정 비용"],
            [run_summary["candidates"], run_summary["rerank_skipped"], run_summary["extraction_failed"],
             run_summary["accepted"], run_summary["discarded"],
             f"${run_summary['llm_cost_usd']:.6f}"],
        ):
            col.metric(label, value)

        targets = [row["taxonomy_lv2_candidate"] for row in query(db_path, """
            SELECT DISTINCT taxonomy_lv2_candidate FROM url_candidates
            WHERE run_id=? AND taxonomy_lv2_candidate IS NOT NULL ORDER BY taxonomy_lv2_candidate
        """, (run_id,))]
        target_filter = st.selectbox("Target taxonomy", ["전체"] + targets, key="phase2_target")
        target_clause = "" if target_filter == "전체" else " AND taxonomy_lv2_candidate=?"
        target_params = (run_id,) if target_filter == "전체" else (run_id, target_filter)

        candidate_view, stored_view = st.tabs(["발견 후보·처리 상태", "저장 콘텐츠"])
        with candidate_view:
            candidate_rows = query(db_path, f"""
                SELECT title,taxonomy_lv2_candidate AS target_taxonomy,status,
                       ROUND(discovery_relevance_score,2) AS discovery_score,
                       ROUND(korea_relevance_score,2) AS korea_score,
                       filter_reason,content_hint,source_url
                FROM url_candidates WHERE run_id=?{target_clause}
                ORDER BY rowid DESC LIMIT 500
            """, target_params)
            st.dataframe(candidate_rows, width="stretch", hide_index=True,
                column_config={
                    "source_url": st.column_config.LinkColumn("URL", display_text="열기"),
                    "content_hint": st.column_config.TextColumn("Tavily snippet", width="large"),
                })

        with stored_view:
            stored_rows = query(db_path, f"""
                SELECT content_id,title,taxonomy_lv2_candidate AS target_taxonomy,
                       taxonomy_lv2 AS predicted_taxonomy,category,action,
                       ROUND(taxonomy_fit_score,2) AS taxonomy_fit,
                       ROUND(korea_relevance_score,2) AS korea_relevance,
                       llm_total_tokens,ROUND(llm_estimated_cost_usd,6) AS llm_cost_usd,source_url
                FROM content_records WHERE run_id=?{target_clause}
                ORDER BY rowid DESC LIMIT 500
            """, target_params)
            st.dataframe(stored_rows, width="stretch", hide_index=True,
                column_config={"source_url": st.column_config.LinkColumn("URL", display_text="열기")})
            if stored_rows:
                stored_options = {
                    f"{row['title'] or '(제목 없음)'} · {row['action']} · {row['content_id'][:8]}": row["content_id"]
                    for row in stored_rows
                }
                stored_selected = st.selectbox("본문 상세 보기", stored_options, key="phase2_content_detail")
                stored_detail = query(db_path, """
                    SELECT title,masked_text,masked_comments,action,filter_reason,
                           taxonomy_lv2_candidate,taxonomy_lv2,category,classification_reason,
                           llm_model,llm_input_tokens,llm_output_tokens,llm_total_tokens,
                           llm_estimated_cost_usd,source_url
                    FROM content_records WHERE content_id=?
                """, (stored_options[stored_selected],))[0]
                st.markdown(f"#### {stored_detail['title'] or '(제목 없음)'}")
                st.caption(
                    f"target `{stored_detail['taxonomy_lv2_candidate']}` → predicted "
                    f"`{stored_detail['taxonomy_lv2'] or '미분류'}` · `{stored_detail['action']}` · "
                    f"[원문 열기]({stored_detail['source_url']})"
                )
                if stored_detail["classification_reason"] or stored_detail["filter_reason"]:
                    st.info(stored_detail["classification_reason"] or stored_detail["filter_reason"])
                with st.expander("실제 페이지 추출 본문 (masked)", expanded=True):
                    st.write(stored_detail["masked_text"] or "(본문 없음)")
                st.caption(
                    f"LLM `{stored_detail['llm_model'] or '—'}` · 입력 {stored_detail['llm_input_tokens'] or 0:,} · "
                    f"출력 {stored_detail['llm_output_tokens'] or 0:,} · 합계 {stored_detail['llm_total_tokens'] or 0:,} tokens · "
                    f"추정 ${(stored_detail['llm_estimated_cost_usd'] or 0):.6f}"
                )

with overview:
    accepted_count = scalar(db_path, "SELECT COUNT(*) AS n FROM content_records WHERE action='accepted'")
    discard_count = scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status IN ('prefilter_discarded','trend_discard','matched_fail')")
    pii_count = scalar(db_path, "SELECT COUNT(*) AS n FROM content_records WHERE pii_detected=1")
    cols = st.columns(5)
    for col, label, value in zip(
        cols,
        ["URL 후보", "저장 콘텐츠", "✅ accepted", "🗑️ discard", "PII 탐지"],
        [total_candidates, total_content, accepted_count, discard_count, pii_count],
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
           WHERE status IN ('extracted','quality_failed','matched_pass',
                            'matched_fail','duplicate','out_of_date_range',
                            'trend_accepted','trend_excluded','trend_pending','trend_discard')""",
    )
    accepted_final = scalar(
        db_path,
        "SELECT COUNT(*) AS n FROM url_candidates WHERE status IN ('matched_pass','trend_accepted')")
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
    stage_cols[3].metric("최종 확보", scalar(db_path, "SELECT COUNT(*) AS n FROM url_candidates WHERE status='trend_accepted'"))
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
    # 최종 콘텐츠에는 accepted만 저장하고 discard는 후보 로그에만 남긴다.
    mc = st.columns(3)
    mc[0].metric("전체 저장", _n("WHERE COALESCE(is_supplementary,0)=0"))
    mc[1].metric("✅ accepted", _n("WHERE action='accepted' AND COALESCE(is_supplementary,0)=0"))
    mc[2].metric("🔗 보조 콘텐츠", _n("WHERE is_supplementary=1"))

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
    action_sel = f3.selectbox("처리 상태", ["전체", "accepted"])
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
