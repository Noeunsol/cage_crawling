"""수집 관찰 + 실행 대시보드 (Streamlit).

읽기 전용이 아니다: .env에 API 키를 쓰고, 열람하는 DB를 최신 스키마로 마이그레이션하며,
1차/2차 수집을 실제로 실행해 검색 크레딧과 OpenAI 비용을 쓴다.

탭 구성은 코드 구조를 그대로 따라간다.
  통합 보기  → ui/integrated_view.py  (같은 SQLite를 합쳐 읽기만 한다)
  ① 1차 수집 → ui/phase1_view.py      (src/phase1)
  ② 2차 수집 → ui/phase2_view.py      (src/phase2)
이 파일은 페이지 설정·사이드바·탭 배치만 하고 화면 로직은 담지 않는다.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st
from dotenv import load_dotenv, set_key

# 로컬 운영 UI에서는 프로젝트 .env가 정본이다. 이미 떠 있는 셸의 오래된 키보다 우선한다.
_ENV_PATH = Path(__file__).with_name(".env")
load_dotenv(_ENV_PATH, override=True)

from src.common import paths  # noqa: E402
from src.common.logging_setup import setup_logging  # noqa: E402
from ui import integrated_view, phase1_view, phase2_view  # noqa: E402
from ui.common import dependency_status  # noqa: E402

setup_logging(component="streamlit")

st.set_page_config(page_title="CAGE 콘텐츠 수집", page_icon="🕸️", layout="wide")
st.title("CAGE 콘텐츠 수집")
st.caption("1차 트렌드 탐색 → taxonomy 커버리지 확인 → 2차 부족분 보강 · raw 원문은 표시하지 않습니다.")

top_integrated, top_phase1, top_phase2 = st.tabs(["통합 보기", "① 1차 수집", "② 2차 수집"])
with top_integrated:
    integrated_tabs = st.tabs(
        ["Taxonomy 현황", "전체 요약", "콘텐츠 탐색", "URL 후보", "토큰·비용", "실패 분석"]
    )
with top_phase1:
    phase1_tabs = st.tabs(["1차 트렌드 수집", "최근 실행", "사전 필터", "수집원 비교"])
with top_phase2:
    phase2_tabs = st.tabs(["2차 Taxonomy 수집", "2차 수집 결과"])

# ── 사이드바: 결과 DB 선택 + API 키 상태 ──
st.sidebar.header("실행 설정")
# 2차 실행이 저장한 DB로 결과 뷰어를 자동 전환 (위젯 생성 전에만 세션값 갱신 가능)
if "_pending_result_db" in st.session_state:
    st.session_state["result_db"] = st.session_state.pop("_pending_result_db")
    st.session_state.pop("result_db_choice", None)
st.session_state.setdefault("result_db", paths.DB_DEFAULT)
_db_options = {
    "통합 DB · content.db": paths.DB_DEFAULT,
    "2차 실험 DB · phase2_pilot.db": paths.PHASE2_DB_DEFAULT,
    "직접 경로 입력": None,
}
_current_db = st.session_state["result_db"]
_default_db_choice = next((label for label, path in _db_options.items() if path == _current_db), "직접 경로 입력")
_db_choice = st.sidebar.radio(
    "결과 DB", list(_db_options), index=list(_db_options).index(_default_db_choice),
    key="result_db_choice", help="통합 DB는 1·2차 결과를 함께, 2차 실험 DB는 Tavily pilot만 확인합니다.",
)
db_path = _db_options[_db_choice] or st.sidebar.text_input(
    "직접 DB 경로", value=_current_db, key="result_db_custom")
st.session_state["result_db"] = db_path

with st.sidebar.expander("API 연결 상태", expanded=True):
    st.caption("키 값은 표시·저장하지 않습니다. 프로젝트 루트 `.env`에 설정한 뒤 다시 읽으세요.")
    for label, module, env_name in (
        ("Tavily", "tavily", "TAVILY_API_KEY"),
        ("OpenAI", "openai", "OPENAI_API_KEY"),
        ("SerpAPI", "serpapi", "SERPAPI_KEY"),
    ):
        ready, reason = dependency_status(module, env_name)
        if ready:
            st.success(f"{label} 준비 완료")
        else:
            st.warning(f"{label} 준비 안 됨 · {reason}")
    st.code("TAVILY_API_KEY=...\nOPENAI_API_KEY=...\nSERPAPI_KEY=...", language="bash")
    with st.form("api_key_form", clear_on_submit=True):
        _keys = {
            "TAVILY_API_KEY": st.text_input("Tavily API Key", type="password"),
            "OPENAI_API_KEY": st.text_input("OpenAI API Key", type="password"),
            "SERPAPI_KEY": st.text_input("SerpAPI Key", type="password"),
        }
        save_keys = st.form_submit_button("입력한 API 키 저장")
    if save_keys:
        for env_name, value in _keys.items():
            if value.strip():   # 빈 입력으로 기존 키를 지우지 않는다.
                set_key(str(_ENV_PATH), env_name, value.strip(), quote_mode="never")
        if any(v.strip() for v in _keys.values()):
            load_dotenv(_ENV_PATH, override=True)
            st.cache_data.clear()
            st.success("입력한 API 키를 .env에 저장했습니다.")
            st.rerun()
        else:
            st.info("저장할 API 키를 하나 이상 입력하세요.")

refresh_col, env_col = st.sidebar.columns(2)
if refresh_col.button("새로고침"):
    st.cache_data.clear()
if env_col.button(".env 다시 읽기"):
    load_dotenv(_ENV_PATH, override=True)
    st.cache_data.clear()
    st.rerun()

# ── 화면 렌더 ──
_view_args = dict(db_path=db_path, trend_cfg=paths.TREND_CONFIG, taxo_cfg=paths.TAXONOMY_CONFIG,
                  settings_cfg=paths.SETTINGS_CONFIG, p2_config=paths.PHASE2_CONFIG)

# 1차 수집 실행 탭은 DB가 없어도 떠야 한다 — 첫 수집의 진입점이기 때문이다.
phase1_view.render_collection(phase1_tabs, **_view_args)

if not Path(db_path).is_file():
    st.info(f"`{db_path}`가 아직 없습니다. **① 1차 수집** 탭에서 시작하거나 CLI로 크롤러를 돌린 뒤 새로고침하세요.")
    st.stop()

# 구 스키마 DB를 현재 컬럼으로 맞춘다(비파괴 ADD COLUMN). 신규 컬럼 조회 가능하게.
try:
    from src.common.storage.store import Store
    Store(db_path).close()
except Exception as exc:  # noqa: BLE001 (마이그레이션 실패해도 조회는 시도)
    st.warning(f"스키마 자동 정렬 건너뜀: {exc}")

phase1_view.render_reports(phase1_tabs, **_view_args)
phase2_view.render(phase2_tabs, **_view_args)
integrated_view.render(integrated_tabs, **_view_args)
