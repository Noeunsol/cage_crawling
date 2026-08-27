"""모든 Streamlit 페이지가 공유하는 설정/DB 접근과 작은 헬퍼들.

app.py와 ui/pages/*.py는 서로 독립적으로 실행되는 스크립트라 Python 변수를 직접 공유할 수 없다.
그래서 "설정 읽기", "DB 연결", "타입별 승인 건수 조회" 같은 공통 로직을 여기 모아두고
각 페이지가 이 모듈을 import해서 재사용한다.
"""

from __future__ import annotations

import calendar
import html as html_lib
import math
import os
import sqlite3
from datetime import date, timedelta

import streamlit as st
import yaml
from dotenv import load_dotenv, set_key

from src.config.loader import PROJECT_ROOT, load_all_configs
from src.config.validator import ConfigError, check_provider_api_keys, validate_configs
from src.storage.database import connect

ENV_PATH = PROJECT_ROOT / ".env"

CONFIG_DIR = PROJECT_ROOT / "configs"


@st.cache_resource(show_spinner=False)
def get_configs() -> dict:
    """configs/*.yaml을 읽고 검증한다. 문제가 있으면 화면에 에러를 띄우고 앱을 멈춘다."""
    configs = load_all_configs()
    try:
        validate_configs(configs)
    except ConfigError as e:
        st.error("**설정 파일에 문제가 있습니다.** configs/*.yaml을 확인해주세요.")
        for issue in e.issues:
            st.markdown(f"- {issue}")
        st.stop()
    return configs


def get_db() -> sqlite3.Connection:
    """세션마다 별도 커넥션을 하나씩 준다 (st.session_state에 보관).

    st.cache_resource로 프로세스 전역에 커넥션 하나를 공유하면, 브라우저 탭(세션)마다
    Streamlit이 다른 스레드에서 스크립트를 돌리면서 같은 sqlite3.Connection 객체의
    트랜잭션 상태를 동시에 건드려 "cannot commit - no transaction is active" 에러가 난다.
    세션별로 커넥션을 분리하면 각 세션 안에서는 항상 순차 실행이라 이 문제가 없다.
    """
    if "db_conn" not in st.session_state:
        configs = get_configs()
        db_path = PROJECT_ROOT / configs["app"]["database"]["path"]
        st.session_state["db_conn"] = connect(db_path)
    return st.session_state["db_conn"]


def reload_configs() -> None:
    """도메인/블랙리스트 등 YAML을 UI에서 고친 뒤 캐시를 비우고 새로 읽게 한다."""
    get_configs.clear()


def save_yaml(relative_name: str, data: dict) -> None:
    """configs/{relative_name}.yaml을 통째로 다시 쓴다 (도메인/블랙리스트 편집용)."""
    path = CONFIG_DIR / f"{relative_name}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, width=100)
    reload_configs()


@st.cache_data(ttl=60, show_spinner=False)
def get_serpapi_usage(api_key: str) -> dict | None:
    """SerpAPI 계정 사용량 조회 (검색 크레딧을 쓰지 않는 계정 정보 API, /account.json).

    실패하면 None을 돌려준다 — 이 화면의 다른 기능을 막을 이유는 아니라서 예외를 올리지 않는다.
    """
    try:
        from serpapi import Client
        return Client(api_key=api_key).account()
    except Exception:
        return None


def show_missing_api_key_banner(providers_cfg: dict) -> None:
    missing = check_provider_api_keys(providers_cfg)
    if missing:
        st.warning(
            "**API key가 설정되지 않았습니다.** 왼쪽 사이드바의 'API Key 상태'에서 확인·입력할 수 있습니다.\n\n"
            + "\n".join(f"- {m}" for m in missing)
        )


def render_api_key_panel(providers_cfg: dict) -> None:
    """사이드바에 provider별 API key 상태를 보여주고, 없는 키는 그 자리에서 입력할 수 있게 한다.

    입력한 값은 즉시 이번 세션(프로세스)에 적용된다. ".env에도 저장" 체크 시 파일에도 남아서
    다음에 서버를 재시작해도 유지된다.
    """
    missing_now = check_provider_api_keys(providers_cfg)
    with st.sidebar.expander("🔑 API Key 상태", expanded=bool(missing_now)):
        for provider in ("openai", "tavily", "serpapi"):
            env_name = providers_cfg[provider]["api_key_env"]
            is_set = bool(os.environ.get(env_name))
            st.write(f"{'✅' if is_set else '❌'} **{provider}** (`{env_name}`)")

        if st.button("🔄 .env 다시 읽기", key="reload_env_btn", width="stretch"):
            load_dotenv(ENV_PATH, override=True)
            reload_configs()
            st.rerun()

        for provider in ("openai", "tavily", "serpapi"):
            env_name = providers_cfg[provider]["api_key_env"]
            if os.environ.get(env_name):
                continue
            st.divider()
            value = st.text_input(
                f"{provider} key 직접 입력", type="password", key=f"manual_key_{provider}",
            )
            persist = st.checkbox(".env 파일에도 저장", key=f"persist_key_{provider}")
            if st.button(f"{provider} 적용", key=f"apply_key_{provider}"):
                if not value:
                    st.error("값을 입력해주세요.")
                else:
                    os.environ[env_name] = value
                    if persist:
                        set_key(str(ENV_PATH), env_name, value)
                    reload_configs()
                    st.success(f"{provider} key를 적용했습니다.")
                    st.rerun()


# LV1(대분류)을 한눈에 구분하기 위한 이모지 배지. 실제 위험도 순위가 아니라 시각적 구분용이다.
LV1_EMOJI = {
    "Toxicity Harms": "🧨",
    "Unfair Representation": "⚖️",
    "Misinformation Harms": "📰",
    "Information and Safety Harms": "🔐",
    "Malicious Use": "🚨",
    "Security and System Integrity Threats": "🛡️",
}


def lv1_badge(lv1_name: str) -> str:
    return f"{LV1_EMOJI.get(lv1_name, '🔹')} {lv1_name}"


def taxonomy_groups(configs: dict) -> list[dict]:
    return configs["taxonomy"]["taxonomy"]


def groups_by_lv1(configs: dict) -> dict[str, list[dict]]:
    """LV1 -> 그 아래 LV2 그룹 목록. taxonomy.yaml에 나온 순서를 그대로 유지한다."""
    result: dict[str, list[dict]] = {}
    for g in taxonomy_groups(configs):
        result.setdefault(g["lv1_name"], []).append(g)
    return result


# retry_policy.yaml의 reason code를 사람이 읽을 한글 라벨로. 목록에 없는 코드는 원문 그대로 보여준다.
EXCLUSION_REASON_LABELS = {
    "blacklisted_domain": "블랙리스트 도메인",
    "date_out_of_range": "기간 범위 밖",
    "low_korea_relevance": "한국 관련성 낮음",
    "taxonomy_mismatch": "taxonomy 부적합",
    "duplicate": "중복",
    "timeout": "요청 시간 초과",
    "temporary_http_error": "일시적 HTTP 오류",
    "access_denied": "접근 거부",
    "not_found": "존재하지 않는 페이지",
    "extraction_failed": "본문 추출 실패",
    "unexpected_error": "예상 못 한 오류",
}


def exclusion_reason_label(decision: str | None, decision_reason: str | None) -> str:
    """콘텐츠 목록 표의 '제외 사유' 컬럼용. decision_reason은 '{reason_code}: {상세}' 형식이라 앞부분만 뽑는다."""
    if decision != "excluded" or not decision_reason:
        return ""
    code = decision_reason.split(":", 1)[0].strip()
    return EXCLUSION_REASON_LABELS.get(code, code)


def render_content_box(text: str, height: int = 300) -> None:
    """수집된 본문을 보여준다. st.text_area(disabled=True)는 항상 회색으로 흐리게 나와서,
    대신 테마 기본 글자색을 그대로 쓰는 스크롤 가능한 div로 그린다 (라이트/다크 모두 잘 보임)."""
    escaped = html_lib.escape(text)
    st.markdown(
        f"<div style='max-height:{height}px; overflow-y:auto; white-space:pre-wrap; "
        "padding:0.75rem; border:1px solid rgba(128,128,128,0.35); border-radius:0.5rem; "
        f"line-height:1.6;'>{escaped}</div>",
        unsafe_allow_html=True,
    )


def find_type(configs: dict, lv2_id: str, type_name: str) -> dict | None:
    for group in taxonomy_groups(configs):
        if group["lv2_id"] == lv2_id:
            for t in group["types"]:
                if t["name"] == type_name:
                    return t
    return None


def accepted_counts(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    """LV2/type별 이미 DB에 쌓여 있는 accepted 콘텐츠 수 (7.2절: 기존 보유량 표시용)."""
    rows = conn.execute(
        """
        SELECT m.taxonomy_lv2, m.type_name, COUNT(*) AS cnt
        FROM content_taxonomy_mappings m
        JOIN contents c ON c.id = m.content_id
        WHERE c.status = 'accepted' AND m.decision = 'accepted'
        GROUP BY m.taxonomy_lv2, m.type_name
        """
    ).fetchall()
    return {(r["taxonomy_lv2"], r["type_name"]): r["cnt"] for r in rows}


def candidate_target(target_count: int, multiplier: float) -> int:
    return math.ceil(target_count * multiplier)


def per_type_target_count(target_count: int, num_types: int) -> int:
    """target_count는 LV2 기준 목표다 — 같은 LV2의 type들이 나눠 갖는다 (나머지는 올림)."""
    return math.ceil(target_count / num_types) if num_types > 0 else 0


def default_date_range(years: int) -> tuple[date, date]:
    today = date.today()
    return today.replace(year=today.year - years), today


def _subtract_months(d: date, months: int) -> date:
    month_index = d.month - 1 - months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])   # 말일 넘어가는 경우 클램프
    return date(year, month, day)


def default_date_range_for_lv2(configs: dict, lv2_id: str) -> tuple[date, date] | None:
    """collection.yaml에 이 LV2 전용 기본 기간(개월)이 있으면 계산해서 돌려주고, 없으면 None."""
    months = configs["collection"]["dates"].get("date_range_months_by_lv2", {}).get(lv2_id)
    if months is None:
        return None
    today = date.today()
    return _subtract_months(today, months), today


def init_setup_state(configs: dict) -> None:
    """수집 설정 기본값을 session_state에 한 번만 채워 넣는다."""
    if "setup" in st.session_state:
        return
    defaults = configs["collection"]["defaults"]
    ratio = configs["collection"]["provider_ratio"]["default"]
    date_from, date_to = default_date_range(defaults["date_range_years"])
    st.session_state["setup"] = {
        "selected_lv2": [],
        "type_enabled": {},          # f"{lv2_id}::{type_name}" -> bool
        "target_count": defaults["target_count"],
        "candidate_multiplier": defaults["candidate_multiplier"],
        "date_from": date_from,
        "date_to": date_to,
        "provider_ratio": dict(ratio),
        "date_overrides": {},           # lv2_id -> {"date_from": date, "date_to": date}
        "provider_ratio_overrides": {},  # lv2_id -> {"tavily": int, "serpapi": int}
        "confirmed": False,
    }


def effective_date_range(setup: dict, configs: dict, lv2_id: str) -> tuple[date, date]:
    """우선순위: 사용자가 화면에서 직접 override한 기간 > collection.yaml의 LV2별 확정 기간 > 전역 기본값 (8.1절)."""
    manual_override = setup.get("date_overrides", {}).get(lv2_id)
    if manual_override:
        return manual_override["date_from"], manual_override["date_to"]
    lv2_default = default_date_range_for_lv2(configs, lv2_id)
    if lv2_default:
        return lv2_default
    return setup["date_from"], setup["date_to"]


def effective_provider_ratio(setup: dict, configs: dict, lv2_id: str) -> dict:
    """우선순위: 사용자가 화면에서 직접 override한 값 > collection.yaml의 LV2별 확정 비율 > 전역 기본값 (7.6절)."""
    manual_override = setup.get("provider_ratio_overrides", {}).get(lv2_id)
    if manual_override:
        return manual_override
    by_lv2 = configs["collection"]["provider_ratio"].get("by_lv2", {})
    return by_lv2.get(lv2_id, setup["provider_ratio"])


def suggested_query_counts(configs: dict, setup: dict, lv2_id: str, num_types_in_lv2: int = 1) -> tuple[int, int]:
    """수집 설정(target_count/candidate_multiplier/provider_ratio)으로 검색어 생성 개수를 역산한다.

    target_count는 LV2 기준 목표라 이 LV2의 type 수만큼 먼저 나눈 뒤(나머지는 올림) 콜당
    최대 결과 수로 나눈 최소 호출 수에 safety_factor를 곱한다 — 실제로는 중복·필터링 탓에
    콜당 수확이 이론치보다 낮다. setup이 없으면(수집 설정 미확정) query_generation.*_count_per_type
    기본값으로 대신한다.
    """
    qgen_cfg = configs["collection"]["query_generation"]
    if not setup:
        return qgen_cfg["tavily_count_per_type"], qgen_cfg["serpapi_count_per_type"]

    per_type = per_type_target_count(setup["target_count"], num_types_in_lv2)
    total = candidate_target(per_type, setup["candidate_multiplier"])
    ratio = effective_provider_ratio(setup, configs, lv2_id)
    tavily_target = round(total * ratio["tavily"] / 100)
    serpapi_target = total - tavily_target

    def _suggest(provider_target: int, provider: str, floor: int) -> int:
        if provider_target <= 0:
            return 0
        max_results = configs["providers"][provider]["max_results_per_request"]
        return max(floor, math.ceil(provider_target / max_results * qgen_cfg["safety_factor"]))

    return (
        _suggest(tavily_target, "tavily", qgen_cfg["tavily_count_per_type"]),
        _suggest(serpapi_target, "serpapi", qgen_cfg["serpapi_count_per_type"]),
    )


def type_key(lv2_id: str, type_name: str) -> str:
    return f"{lv2_id}::{type_name}"


def type_option_label(configs: dict, lv2_id: str, type_name: str) -> str:
    """선택 위젯에서 'lv2_id::type_name' 대신 사람이 읽기 쉬운 라벨을 보여줄 때 쓴다."""
    group = next((g for g in taxonomy_groups(configs) if g["lv2_id"] == lv2_id), None)
    lv2_name = group["lv2_name"] if group else lv2_id
    badge = lv1_badge(group["lv1_name"]) if group else "🔹"
    return f"{badge} › {lv2_name} › {type_name}"


def selected_types(configs: dict) -> list[tuple[str, str]]:
    """setup.confirmed 이후, 사용자가 활성화한 (lv2_id, type_name) 목록."""
    setup = st.session_state.get("setup", {})
    enabled = setup.get("type_enabled", {})
    result = []
    for group in taxonomy_groups(configs):
        if group["lv2_id"] not in setup.get("selected_lv2", []):
            continue
        for t in group["types"]:
            if enabled.get(type_key(group["lv2_id"], t["name"]), True):
                result.append((group["lv2_id"], t["name"]))
    return result
