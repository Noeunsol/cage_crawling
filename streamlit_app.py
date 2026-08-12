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
from src.pipelines.stages import _load_llm_cfg  # noqa: E402  (crawler_settings + configs/llm.yaml 병합)
from src.policy import load_policies  # noqa: E402
setup_logging(component="streamlit")

def _fmt_dur(sec: float) -> str:
    """소요 시간 사람이 읽기 좋게. 60s 미만은 초, 이상은 m s."""
    sec = round(sec)
    return f"{sec}s" if sec < 60 else f"{sec // 60}m {sec % 60}s"


def _tavily_run_label(run_id: str) -> str:
    """새 run ID는 실행 시각을 표시하고, 기존 UUID 이력도 그대로 읽는다."""
    parts = run_id.split("_")
    if len(parts) == 4 and parts[0] == "tavily" and len(parts[1]) == 8 and len(parts[2]) == 6:
        day, clock = parts[1], parts[2]
        if day.isdigit() and clock.isdigit():
            return f"{day[:4]}-{day[4:6]}-{day[6:]} {clock[:2]}:{clock[2:4]}:{clock[4:]} · {run_id}"
    return f"기존 이력 · {run_id}"


# 최종 판정: accepted/discard · 1차: keep/discard
_ACTION_LABEL = {
    "accepted": "✅ accepted", "discard": "🗑️ discard",
    "keep": "📥 keep", "discard": "🗑️ discard",
}
_STATUS_LABEL = {
    "rerank_skipped": "검색 단계에서 제외",
    "prefilter_discarded": "제목 단계 제외",
    "sampling_skipped": "날짜·시간 표본 미선택",
    "extraction_failed": "본문 수집 실패",
    "trend_discard": "본문 확인 후 제외",
    "trend_excluded": "이전 버전 LLM 제외",
    "trend_accepted": "2차 후보 저장",
    "trend_candidate": "Tavily 미검수 후보",
    "trend_review": "이전 버전 검토 상태",
    "trend_pending": "이전 버전 분류 대기",
    "duplicate": "중복 콘텐츠",
    "supplementary_collected": "보조 링크 수집 완료",
    "discovered": "발견",
}
_METHOD_LABEL = {
    "board_list": "게시판 목록",
    "rss": "뉴스 RSS",
    "in_body_link": "본문 내부 링크",
}

st.set_page_config(page_title="CAGE 콘텐츠 수집", page_icon="🕸️", layout="wide")
st.title("CAGE 콘텐츠 수집")
st.caption("1차 트렌드 탐색 → taxonomy 커버리지 확인 → 2차 부족분 보강 · raw 원문은 표시하지 않습니다.")
top_integrated, top_phase1, top_phase2 = st.tabs(["통합 보기", "① 1차 수집", "② 2차 수집"])
with top_integrated:
    taxonomy_tab, overview, content_tab, candidates_tab, llm_tab, failures_tab, pii_tab = st.tabs(
        ["Taxonomy 현황", "전체 요약", "콘텐츠 탐색", "URL 후보", "토큰·비용", "실패 분석", "PII"]
    )
with top_phase1:
    trend_collection_tab, recent_tab, prefilter_tab = st.tabs(["1차 트렌드 수집", "최근 실행", "사전 필터"])
with top_phase2:
    phase2_tab, phase2_results_tab = st.tabs(["2차 Tavily 수집", "Tavily 수집 결과"])


@st.cache_data(ttl=5)
def query(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(db_path: str, sql: str, params: tuple = ()) -> int:
    rows = query(db_path, sql, params)
    return next(iter(rows[0].values())) if rows else 0



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
    st.session_state.pop("result_db_choice", None)
st.session_state.setdefault("result_db", "data/db/content.db")
_db_options = {
    "통합 DB · content.db": "data/db/content.db",
    "2차 실험 DB · phase2_pilot.db": "data/db/phase2_pilot.db",
    "직접 경로 입력": None,
}
_current_db = st.session_state["result_db"]
_default_db_choice = next((label for label, path in _db_options.items() if path == _current_db), "직접 경로 입력")
_db_choice = st.sidebar.radio(
    "결과 DB", list(_db_options), index=list(_db_options).index(_default_db_choice),
    key="result_db_choice", help="통합 DB는 1·2차 결과를 함께, 2차 실험 DB는 Tavily pilot만 확인합니다.",
)
if _db_options[_db_choice]:
    db_path = _db_options[_db_choice]
else:
    db_path = st.sidebar.text_input("직접 DB 경로", value=_current_db, key="result_db_custom")
st.session_state["result_db"] = db_path
trend_cfg = st.sidebar.text_input("1차 수집 config", "configs/trend_collection.yaml")
taxo_cfg = st.sidebar.text_input("Taxonomy config", "configs/taxonomy.yaml")
settings_cfg = st.sidebar.text_input("공통 crawler config", "configs/crawler_settings.yaml")
p2_config = st.sidebar.text_input("2차 수집 config", "configs/targeted_collection.yaml")
refresh_col, env_col = st.sidebar.columns(2)
if refresh_col.button("새로고침"):
    st.cache_data.clear()
if env_col.button(".env 다시 읽기"):
    load_dotenv(_ENV_PATH, override=True)
    st.cache_data.clear()
    st.rerun()

with trend_collection_tab:
    st.subheader("1차 트렌드 수집")
    st.caption("수집 기간과 수집원을 정한 뒤, 미리보기 또는 실제 수집을 실행합니다.")
    st.markdown("**수집 기간 · 수집원**")
    use_dc = st.checkbox("디시인사이드", value=True)
    use_news = st.checkbox("뉴스 RSS", value=True)
    st.caption("FM코리아·네이트판은 다음 단계 지원 예정")
    # 상한은 trend_collection.yaml의 collection.max_lookback_days가 정본이다.
    # UI에서만 올리면 파이프라인이 조용히 잘라내므로 그 값을 그대로 읽어 쓴다.
    try:
        _trend_cfg = yaml.safe_load(Path(trend_cfg).read_text(encoding="utf-8")) or {}
        _max_lookback = int(_trend_cfg.get("collection", {}).get("max_lookback_days", 30))
        _daily_quota = _trend_cfg.get("sampling", {}).get("daily_quota_by_source", {})
        _default_dc_daily_cap = int(_daily_quota.get("dcinside", 50))
        _default_news_daily_cap = int(_daily_quota.get("news_rss", 30))
    except Exception:  # noqa: BLE001
        _max_lookback = 30
        _default_dc_daily_cap, _default_news_daily_cap = 50, 30
    lookback_days = st.number_input(
        "최근 며칠", min_value=1, max_value=_max_lookback, value=1, step=1,
        help=f"오늘부터 거슬러 며칠분을 수집할지. 설정 파일 상한은 {_max_lookback}일입니다.")
    st.metric("수집 범위", f"최근 {int(lookback_days)}일")
    dc_daily_cap = st.number_input(
        "디시 1일 최대 수집",
        min_value=0,
        max_value=1000,
        value=_default_dc_daily_cap,
        step=10,
        help="하루에 디시인사이드에서 본문 수집할 최대 건수입니다. 0이면 수집하지 않습니다.",
    )
    news_daily_cap = st.number_input(
        "뉴스 RSS 1일 최대 수집",
        min_value=0,
        max_value=1000,
        value=_default_news_daily_cap,
        step=10,
        help="하루에 뉴스 RSS에서 본문 수집할 최대 건수입니다. 0이면 수집하지 않습니다.",
    )
    source_daily = (dc_daily_cap if use_dc else 0) + (news_daily_cap if use_news else 0)
    st.caption(
        f"본문 후보 상한 {source_daily * int(lookback_days):,}건 "
        f"(디시 날짜당 {int(dc_daily_cap)} · 뉴스 날짜당 {int(news_daily_cap)}, 4시간대 균등 표본). "
        "실제 처리 건수는 기간 내 가용 후보에서 중복·제목 필터를 제외한 수입니다."
    )
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
                bar.progress(done / total if total else 1.0,
                             text=f"가용·필터 통과 후보 {done}/{total}건 처리 중…")

            try:
                from src import pipeline
                _t0 = time.perf_counter()
                rep = pipeline.run_trend(trend_config=trend_cfg, taxonomy_config=taxo_cfg,
                                         settings_config=settings_cfg, db_path=db_path, reset_db=reset,
                                         overrides=overrides, on_progress=_prog)
                _elapsed = time.perf_counter() - _t0
                bar.progress(1.0, text="완료")
                st.cache_data.clear()
                st.success(f"저장 {rep.get('stored_records', 0):,}건 · 소요 {_fmt_dur(_elapsed)} · 최근 실행 탭에서 확인")
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

with recent_tab:
    st.subheader("최근 실행한 1차 수집")
    st.caption("선택한 DB에서 가장 마지막으로 완료한 1차 실행 1건만 표시합니다. 새로고침해도 유지됩니다.")
    recent_runs = query(db_path, """
        SELECT run_id, MAX(rowid) AS latest_rowid,
               COUNT(*) AS candidates,
               SUM(CASE WHEN status='trend_accepted' THEN 1 ELSE 0 END) AS accepted,
               SUM(CASE WHEN status='extraction_failed' THEN 1 ELSE 0 END) AS extraction_failed,
               SUM(CASE WHEN status NOT IN ('trend_accepted', 'discovered', 'extraction_failed') THEN 1 ELSE 0 END) AS excluded
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
              AND status NOT IN ('trend_accepted','discovered')
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
                 "처리 결과": _STATUS_LABEL.get(row["status"], row["status"]),
                 "사유": row["filter_reason"] or "—", "원문": row["source_url"]}
                for row in discarded_rows
            ], hide_index=True, width="stretch",
                column_config={"원문": st.column_config.LinkColumn("원문", display_text="열기")})
    else:
        st.info("실행 ID가 기록된 1차 수집 결과가 없습니다. 위에서 1차 수집을 한 번 실행해 보세요.")

with taxonomy_tab:
    st.subheader("통합 Taxonomy별 콘텐츠 현황")
    st.caption("1·2차에서 저장된 결과를 함께 봅니다. accepted만 유효 커버리지로 계산하며, 2차 수집은 부족분이 큰 LV2부터 보강합니다.")
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
        st.caption("관련성 **0.40 미만** 또는 한국성이 낮은 후보는 검색 단계에서 제외합니다. 그 외 후보는 관련성 점수에 따라 본문 수집 우선순위가 정해집니다.")
        rc = st.columns(2)
        md = rc[0].slider("후보 관련성 우선점수", 0.0, 1.0, 0.65, 0.05,
                          help="이 점수 이상인 후보를 먼저 본문 수집합니다. 미만 후보도 fetch 여유가 있으면 수집·분류합니다.")
        mk = rc[1].slider("후보 한국관련성 최소점수", 0.0, 1.0, 0.60, 0.05,
                          help="한국 콘텐츠일 가능성 기준. 높이면 비한국 후보를 더 걸러냄.")
        ac = st.columns(3)
        af = ac[0].slider("후보 저장·주제 적합도", 0.0, 1.0, 0.45, 0.05,
                          help="선택 taxonomy와 대략적으로 연결되는 최소 점수입니다.")
        ak = ac[1].slider("후보 저장·한국 관련성", 0.0, 1.0, 0.30, 0.05,
                          help="한국어·국내 플랫폼·국내 맥락 중 하나가 확인되는 최소 점수입니다.")
        an = ac[2].slider("후보 저장·구체성", 0.0, 1.0, 0.20, 0.05,
                          help="유해 표현·행동·사례가 본문에 어느 정도 드러나는지의 최소 점수입니다.")
        lc = st.columns(3)
        p2_max_lv2 = lc[0].number_input("한 번에 처리할 카테고리 수", 1, 19, 5,
                                        help="많이 선택해도 이 수만큼만 처리합니다.")
        p2_max_fetch = lc[1].number_input("카테고리당 본문 수집 상한", 1, 100, 20)
        p2_max_classify = lc[2].number_input("LLM 분류 총 상한(비용 상한)", 1, 500, 60,
                                             help="이 횟수를 넘으면 분류를 멈춥니다. 비용 사고 방지.")
        p2_target = st.number_input("카테고리별 목표 건수", 1, 1000, 30,
                                    help="accepted로 확정된 콘텐츠 수를 기준으로 부족분을 계산합니다.")
        p2_verify_openai = st.checkbox("OpenAI taxonomy 검수", value=False,
                                       help="본문 taxonomy 검수만 제어합니다. 끄면 미검수 후보로 저장하며, "
                                            "fetch 전 rerank의 애매밴드 LLM 판정은 이 설정과 무관하게 동작합니다.")
    p2_overrides = {
        "rerank": {"min_discovery_relevance": md, "min_korea_relevance": mk},
        "acceptance": {"min_taxonomy_fit_score": af, "min_korea_relevance_score": ak,
                       "min_concrete_context_score": an},
        "adjudication": {"openai_verification": p2_verify_openai},
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

    try:
        _missing = _p2.missing_manual_intents(load_policies(taxo_cfg),
                                              _p2._load_phase2_config(p2_config))
    except Exception:  # noqa: BLE001
        _missing = []
    if _missing:
        _names = ", ".join(_pol_meta.get(c, (c, ""))[0] for c in _missing)
        st.warning(f"수동 검색문이 없는 카테고리 {len(_missing)}개 — taxonomy에서 자동 생성된 문장으로 "
                   f"검색합니다(품질 낮음). `{p2_config}`에 추가하세요: {_names}")

    def _lv2_label(code: str) -> str:
        name = _pol_meta.get(code, (code, ""))[0]
        d = p2_by_lv2.get(code, {}).get("deficit")
        return f"{name} · 부족 {d:g}건" if d is not None else name

    try:
        _source_presets = (_p2._load_phase2_config(p2_config).get("source_selection_presets") or {})
    except Exception:  # noqa: BLE001
        _source_presets = {}
    preset_keys = list(_source_presets)
    preset_choice = st.radio(
        "2차 수집 출처 추천", ["직접 선택"] + preset_keys, horizontal=True,
        format_func=lambda key: "직접 선택" if key == "직접 선택" else _source_presets[key].get("label", key),
        help="뉴스/커뮤니티 중 어느 쪽에서 더 좋은 원문을 얻기 쉬운지에 따른 taxonomy 추천입니다. Tavily 도메인을 강제로 제한하지는 않습니다.",
    )
    if preset_choice == "직접 선택":
        preset_default = list(p2_by_lv2)[:2]
        preset_key = "manual"
        preset_members = list(p2_by_lv2)
    else:
        preset = _source_presets[preset_choice]
        preset_members = [lv2 for lv2 in p2_by_lv2 if lv2 in set(preset.get("lv2s", []))]
        preset_default = preset_members[:min(2, int(p2_max_lv2))]
        preset_key = preset_choice
        st.caption(f"**{preset.get('label', preset_choice)}** — {preset.get('description', '')}")
        pc1, pc2, pc3 = st.columns(3)
        pc1.metric("추천 taxonomy", len(preset_members))
        pc2.metric("기본 선택", len(preset_default))
        pc3.metric("이번 실행 상한", int(p2_max_lv2))
    selection_key = f"p2_sel_{preset_key}_v2"
    if selection_key not in st.session_state:
        st.session_state[selection_key] = preset_default
    if preset_choice != "직접 선택":
        sc1, sc2, _ = st.columns([1, 1, 3])
        if sc1.button("추천 전체 선택", key=f"p2_select_all_{preset_key}"):
            st.session_state[selection_key] = preset_members
        if sc2.button("부족분 상위로 복원", key=f"p2_select_top_{preset_key}"):
            st.session_state[selection_key] = preset_default
    p2_selected = st.multiselect(
        "보강할 안전 카테고리 (부족분이 큰 순서)", list(p2_by_lv2),
        format_func=_lv2_label, key=selection_key,
        help="1차 수집이 목표치보다 모자란 카테고리입니다. 처음이면 1~2개만 골라 보세요.")
    if len(p2_selected) > int(p2_max_lv2):
        st.info(f"이번 실행은 고급 설정의 카테고리 상한({int(p2_max_lv2)}개)까지, 부족분이 큰 순서로 처리합니다.")
    for _c in p2_selected:
        _desc = _pol_meta.get(_c, ("", ""))[1]
        if _desc:
            st.caption(f"• **{_pol_meta[_c][0]}** — {_desc}")

    wc = st.columns([2, 1, 1])
    p2_scratch = wc[0].text_input("Small Run 저장 DB", "data/db/phase2_pilot.db",
                                  help="기본은 scratch DB. 튜닝 중 content.db를 더럽히지 않는다.")
    p2_use_main = wc[1].checkbox("content.db에 저장", value=False)
    p2_limit = wc[2].number_input("실제 본문 fetch 상한", 1, 60, 12,
                                  help="Tavily API 호출 횟수가 아니라, 검색 후 실제 URL 본문을 가져올 최대 건수입니다. type별 후보를 고르게 확인하려면 9 이상을 권장합니다.")
    p2_write_db = db_path if p2_use_main else p2_scratch
    _searches = sum(len(p2_by_lv2[lv2]["intent"].queries) for lv2 in p2_selected)
    st.caption(f"⚠️ Discovery Preview와 2차 보강 실행은 Tavily 검색을 각각 새로 호출합니다. "
               f"검색어 1개 = 호출 1회이며, 지금 선택으로 실행당 **{_searches}회**(basic 기준 {_searches} credits) "
               f"입니다. fetch 상한과는 별도입니다.")
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
                                "검색어": len(it["intent"].queries),
                                "검증 점수": check["score"], "상태": check["status"],
                                "경고": " · ".join(check["warnings"]) or "없음"})
        st.dataframe(intent_rows, hide_index=True, width="stretch")
        for lv2 in p2_selected:
            it = p2_by_lv2.get(lv2)
            if it:
                intent = it["intent"]
                check = validate_collection_intent(intent)
                st.markdown(f"**{lv2}** · 검색어 {len(intent.queries)}개 · `{check['status']} {check['score']}/100`")
                for q in intent.queries:
                    st.write(f"- {q}")
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
            provider = _p2._default_provider(_p2._load_phase2_config(p2_config))
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
                    query_rows = []
                    for query_text in it["intent"].queries:
                        rows = [e for e in entries if e["result"].query_or_intent == query_text]
                        query_rows.append({
                            "목표 type": it["intent"].query_types.get(query_text) or "LV2 공통",
                            "Tavily query": query_text,
                            "후보": len(rows),
                            "fetch": sum(e["rerank"].fetch_decision == "fetch" for e in rows),
                            "skip": sum(e["rerank"].fetch_decision == "skip" for e in rows),
                        })
                    st.caption("재분류 전 Tavily discovery 품질 — type별 후보·rerank 결과")
                    st.dataframe(query_rows, hide_index=True, width="stretch")
                st.dataframe([{
                    "목표 type": it["intent"].query_types.get(e["result"].query_or_intent) or "LV2 공통",
                    "Tavily query": e["result"].query_or_intent,
                    "title": e["result"].title, "url": e["result"].url,
                    "provider": round(e["result"].provider_score or 0, 2),
                    "discovery": e["rerank"].discovery_relevance_score,
                    "korea": e["rerank"].korea_relevance_score,
                    "decision": e["rerank"].fetch_decision, "by": e["rerank"].source,
                } for e in entries], hide_index=True, width="stretch",
                    column_config={"url": st.column_config.LinkColumn("url", display_text="열기")})
            tu = provider.usage_summary()
            st.caption(f"이번 Preview Tavily 사용량: 검색 호출 {tu['calls']}회 · {tu['credits']} credits")
            if tu["queries"]:
                st.dataframe(tu["queries"], hide_index=True, width="stretch")

    # 3단계 (필수) — 실제 본문 수집·분류·저장, 비용 발생
    with st.container(border=True):
        # 캐시된 LV2는 재검색하지 않는다. 남은 LV2의 검색어 수가 실제 호출 수.
        _will = sum(len(p2_by_lv2[lv2]["intent"].queries)
                    for lv2 in p2_selected if lv2 not in _cached_sel)
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
                    reference_db=db_path,   # scratch에 써도 본 DB 기존분은 재수집하지 않는다
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
                if rep.get("by_query"):
                    st.subheader("검색어별 성과")
                    st.caption("candidate 대비 저장(accepted+candidate) 비율이 낮은 검색어가 "
                               "다음 라운드에 교체할 대상입니다.")
                    st.dataframe([
                        {"검색어": q["query"], "LV2": q["lv2"], "후보": q["candidate_count"],
                         "fetch": q["rerank_fetch_count"], "추출성공": q["extract_success_count"],
                         "accepted": q["accepted"], "candidate": q["candidate"],
                         "discard": q["discard"], "중복": q["duplicate"]}
                        for q in rep["by_query"]
                    ], hide_index=True, width="stretch")
                tu = rep.get("tavily_usage", {})
                st.subheader("Tavily API 사용량")
                uc = st.columns(2)
                uc[0].metric("검색 호출", tu.get("calls", 0))
                uc[1].metric("사용 credits", tu.get("credits", 0))
                if tu.get("queries"):
                    st.dataframe(tu["queries"], hide_index=True, width="stretch")
            except Exception as exc:  # noqa: BLE001
                st.error(f"실행 실패: {exc}")

with phase2_results_tab:
    st.subheader("Tavily 수집 결과")
    st.caption("Tavily 검색 후보, 본문 수집, OpenAI 검수와 저장된 본문을 실행별로 확인합니다.")
    result_runs = query(db_path, """
        SELECT run_id,MAX(rowid) AS latest_row,COUNT(*) AS discovered,
               SUM(CASE WHEN status='rerank_skipped' THEN 1 ELSE 0 END) AS skipped,
               SUM(CASE WHEN status='extraction_failed' THEN 1 ELSE 0 END) AS extract_failed,
               SUM(CASE WHEN status='trend_candidate' THEN 1 ELSE 0 END) AS unverified,
               SUM(CASE WHEN status='trend_accepted' THEN 1 ELSE 0 END) AS accepted,
               SUM(CASE WHEN status='trend_discard' THEN 1 ELSE 0 END) AS discarded
        FROM url_candidates WHERE collection_phase=2 AND run_id IS NOT NULL AND run_id!=''
        GROUP BY run_id ORDER BY latest_row DESC
    """)
    if not result_runs:
        st.info("현재 선택한 DB에는 Tavily 2차 실행 이력이 없습니다.")
    else:
        result_run_id = st.selectbox(
            "실행 이력", [row["run_id"] for row in result_runs], key="phase2_results_run",
            format_func=_tavily_run_label,
        )
        result_summary = next(row for row in result_runs if row["run_id"] == result_run_id)
        result_metrics = st.columns(6)
        for col, label, value in zip(
            result_metrics, ["Tavily 검색 결과", "검색 후보 제외", "본문 수집 실패", "Tavily 미검수 후보", "OpenAI 검수 통과", "본문 확인 후 제외"],
            [result_summary["discovered"], result_summary["skipped"], result_summary["extract_failed"],
             result_summary["unverified"], result_summary["accepted"], result_summary["discarded"]],
        ):
            col.metric(label, value)
        result_targets = [row["taxonomy_lv2_candidate"] for row in query(db_path, """
            SELECT DISTINCT taxonomy_lv2_candidate FROM url_candidates
            WHERE run_id=? AND taxonomy_lv2_candidate IS NOT NULL ORDER BY taxonomy_lv2_candidate
        """, (result_run_id,))]
        result_target = st.selectbox("대상 taxonomy", ["전체"] + result_targets, key="phase2_results_target")
        result_target_clause = "" if result_target == "전체" else " AND taxonomy_lv2_candidate=?"
        result_target_params = (result_run_id,) if result_target == "전체" else (result_run_id, result_target)

        unverified_count = scalar(db_path, f"""
            SELECT COUNT(*) AS n FROM content_records
            WHERE run_id=? AND collection_phase=2 AND action='candidate'
              AND classification_source='tavily_unverified'{result_target_clause}
        """, result_target_params)
        if unverified_count:
            with st.container(border=True):
                st.markdown("#### Tavily 미검수 후보 OpenAI 검수")
                st.caption("Tavily 검색·본문 수집은 다시 하지 않고, 저장된 masked 본문만 OpenAI에 보냅니다.")
                vc1, vc2 = st.columns([1, 2])
                verify_limit = vc1.number_input("검수할 후보 수", 1, int(unverified_count), int(unverified_count),
                                                key="phase2_results_verify_limit")
                if vc2.button("미검수 후보 OpenAI 검수 실행", key="phase2_results_verify", type="primary"):
                    try:
                        from src import pipeline as _p2_verify
                        verify_report = _p2_verify.verify_unverified_candidates(
                            result_run_id, db_path, p2_config, taxonomy_config=taxo_cfg,
                            settings_config=settings_cfg,
                            target_lv2=None if result_target == "전체" else result_target,
                            limit=int(verify_limit), overrides=p2_overrides,
                        )
                        st.cache_data.clear()
                        if verify_report.get("error"):
                            st.error(f"검수를 시작하지 못했습니다: {verify_report['error']}")
                        else:
                            st.success(f"OpenAI 검수 {verify_report['verified']}건 · 채택 {verify_report['accepted']}건 · 제외 {verify_report['discarded']}건")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"기존 후보 OpenAI 검수 실패: {exc}")

        candidate_result_tab, stored_result_tab = st.tabs(["Tavily 검색 후보·처리 결과", "본문 저장 결과"])
        with candidate_result_tab:
            result_status = st.selectbox(
                "결과 상태", ["전체", "Tavily 미검수 후보", "OpenAI 검수 후보", "본문 수집 실패", "검색 단계 제외", "본문 확인 후 제외"],
                key="phase2_results_status",
            )
            status_map = {
                "Tavily 미검수 후보": "trend_candidate", "OpenAI 검수 후보": "trend_accepted",
                "본문 수집 실패": "extraction_failed", "검색 단계 제외": "rerank_skipped", "본문 확인 후 제외": "trend_discard",
            }
            result_status_clause = "" if result_status == "전체" else " AND status=?"
            result_params = result_target_params if result_status == "전체" else (*result_target_params, status_map[result_status])
            result_rows = query(db_path, f"""
                SELECT title,published_at_hint,taxonomy_lv2_candidate,status,
                       ROUND(discovery_relevance_score,2) AS discovery_score,
                       ROUND(korea_relevance_score,2) AS korea_score,filter_reason,source_url
                FROM url_candidates WHERE run_id=?{result_target_clause}{result_status_clause} ORDER BY rowid DESC LIMIT 500
            """, result_params)
            st.dataframe([
                {"제목": row["title"] or "(제목 없음)", "Tavily 제공 발행일": row["published_at_hint"] or "미제공",
                 "대상 taxonomy": row["taxonomy_lv2_candidate"], "처리 결과": _STATUS_LABEL.get(row["status"], row["status"]),
                 "관련성": row["discovery_score"], "한국성": row["korea_score"], "이유": row["filter_reason"] or "—",
                 "원문": row["source_url"]}
                for row in result_rows
            ], width="stretch", hide_index=True,
                column_config={"원문": st.column_config.LinkColumn("원문", display_text="열기")})
        with stored_result_tab:
            stored_rows = query(db_path, f"""
                SELECT title,published_at,taxonomy_lv2_candidate AS target_taxonomy,
                       taxonomy_lv2 AS predicted_taxonomy,category,action,
                       ROUND(taxonomy_fit_score,2) AS taxonomy_fit,
                       ROUND(korea_relevance_score,2) AS korea_relevance,source_url
                FROM content_records WHERE run_id=?{result_target_clause} ORDER BY rowid DESC LIMIT 500
            """, result_target_params)
            st.dataframe(stored_rows, width="stretch", hide_index=True,
                         column_config={"source_url": st.column_config.LinkColumn("원문", display_text="열기")})

with overview:
    st.subheader("통합 · 전체 수집 요약")
    st.caption("1차 트렌드 수집과 2차 Tavily 보강 수집을 합산한 현황입니다. 단계별 상세는 각 전용 탭에서 확인하세요.")
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

with candidates_tab:
    st.subheader("통합 · 후보 URL 처리 현황")
    st.caption("1·2차 후보를 함께 봅니다. 아래에서 수집 단계를 고르면 해당 단계만 확인할 수 있습니다.")
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
    c1, c2, c3 = st.columns(3)
    status = c1.selectbox("처리 결과", statuses, format_func=lambda x: _STATUS_LABEL.get(x, x))
    method = c2.selectbox("발견 경로", methods, format_func=lambda x: _METHOD_LABEL.get(x, x))
    candidate_phase = c3.selectbox("수집 단계", ["전체", "① 1차 수집", "② Tavily 2차"], key="candidate_phase_filter")
    where, params = [], []
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
        "처리 결과": _STATUS_LABEL.get(row["status"], row["status"]),
        "사유": row["filter_reason"] or "—",
        "발견 경로": _METHOD_LABEL.get(row["discovery_method"], row["discovery_method"]),
        "URL": row["source_url"],
        "내부 상태": row["status"],
    } for row in rows]
    st.dataframe(friendly_rows, width="stretch", hide_index=True,
                 column_config={"URL": st.column_config.LinkColumn("URL", display_text="열기")})

with content_tab:
    st.subheader("통합 · 저장 콘텐츠 탐색")
    st.caption("1·2차에서 실제로 저장된 콘텐츠를 함께 봅니다. 수집 단계 필터로 분리해 확인할 수 있습니다.")
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
    f1, f2, f3, f4 = st.columns(4)
    lv2 = f1.selectbox("Taxonomy lv2", ["전체"] + [row["taxonomy_lv2"] for row in lv2_rows])
    source_sel = f2.selectbox("수집원", ["전체"] + [row["source"] for row in src_rows])
    action_sel = f3.selectbox("처리 상태", ["전체", "accepted"])
    content_phase = f4.selectbox("수집 단계", ["전체", "① 1차 수집", "② Tavily 2차"], key="content_phase_filter")
    where, params = ["COALESCE(is_supplementary,0)=0"], []
    if lv2 != "전체":
        where.append("taxonomy_lv2=?"); params.append(lv2)
    if source_sel != "전체":
        where.append("source=?"); params.append(source_sel)
    if action_sel != "전체":
        where.append("action=?"); params.append(action_sel)
    if content_phase == "① 1차 수집":
        where.append("COALESCE(collection_phase,1)=1")
    elif content_phase == "② Tavily 2차":
        where.append("collection_phase=2")
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
            with st.expander(f"댓글 {len(comments)}개 (masked · 과거 수집분)"):
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

with pii_tab:
    st.subheader("공통 · PII 점검")
    st.warning("raw_text와 원문 PII는 이 화면에서 조회하지 않습니다.")
    st.dataframe(query(
        db_path,
        """SELECT title,source_url,subtype,pii_types,ROUND(pii_risk_score,3) AS pii_risk,
                  masking_version,masking_warnings,masked_entities
           FROM content_records WHERE pii_detected=1
           ORDER BY pii_risk_score DESC LIMIT 300""",
    ), width="stretch", hide_index=True,
        column_config={"source_url": st.column_config.LinkColumn("URL")})
