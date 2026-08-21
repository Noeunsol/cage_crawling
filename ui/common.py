"""대시보드 공용 — 읽기 전용 DB 조회, 상태 라벨, 표시 포맷.

여기 있는 것은 세 화면(통합/1차/2차)이 함께 쓰는 것뿐이다.
화면별 로직은 integrated_view / phase1_view / phase2_view에 둔다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import streamlit as st


# ── DB 조회 (읽기 전용) ──

@st.cache_data(ttl=5)
def query(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(db_path: str, sql: str, params: tuple = ()) -> int:
    rows = query(db_path, sql, params)
    return next(iter(rows[0].values())) if rows else 0


def has_column(db_path: str, table: str, column: str) -> bool:
    """구버전 DB도 결과 화면에서 읽을 수 있도록 migration 컬럼 존재 여부를 확인한다."""
    return column in {row["name"] for row in query(db_path, f"PRAGMA table_info({table})")}


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


# ── 표시 포맷 ──

def fmt_dur(sec: float) -> str:
    """소요 시간 사람이 읽기 좋게. 60s 미만은 초, 이상은 m s."""
    sec = round(sec)
    return f"{sec}s" if sec < 60 else f"{sec // 60}m {sec % 60}s"


def fmt_date(value: str | None) -> str:
    """검색 provider 날짜를 YYYY-MM-DD로 표시한다. 파싱 실패 시 원문을 보존한다."""
    text = (value or "").strip()
    if not text:
        return "미제공"
    normalized = text.replace(".", "-").replace("/", "-")
    try:
        return datetime.fromisoformat(normalized[:10]).date().isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return text


def run_label(run_id: str) -> str:
    """2차 provider·taxonomy run ID의 실행 시각을 표시하고, 기존 UUID 이력도 그대로 읽는다."""
    parts = run_id.split("_")
    if len(parts) == 4 and parts[0] in {"tavily", "serpapi", "taxonomy"} and len(parts[1]) == 8 and len(parts[2]) == 6:
        day, clock = parts[1], parts[2]
        if day.isdigit() and clock.isdigit():
            prefix = "Taxonomy 실행" if parts[0] == "taxonomy" else parts[0]
            return f"{day[:4]}-{day[4:6]}-{day[6:]} {clock[:2]}:{clock[2:4]}:{clock[4:]} · {prefix} · {run_id}"
    return f"기존 이력 · {run_id}"


def discovery_cache_key(intent, rerank_cfg: dict) -> str:
    """intent나 rerank 설정이 바뀌면 이전 Tavily 결과를 재사용하지 않는다."""
    return json.dumps({"intent": vars(intent), "rerank": rerank_cfg}, sort_keys=True, ensure_ascii=False)


def collection_rows(stats: dict, preview: bool = False) -> list[dict]:
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


# ── 상태 라벨 ──
# url_candidates.status는 모드 중립이다(1차/2차 구분은 collection_phase 컬럼이 한다).

# 최종 판정: accepted/discard · 1차 relevance gate: keep/discard
ACTION_LABEL = {
    "accepted": "✅ accepted", "discard": "🗑️ discard", "keep": "📥 keep",
}
STATUS_LABEL = {
    # permissive_collection이 켜진 카테고리에서는 rerank 관문이 꺼진다.
    # 옛 실행 기록과 섞이면 "지금도 걸리는 줄" 알게 되므로 라벨에 표시한다.
    # quality는 permissive여도 '한국어 아님'만은 항상 막는다(hard reject).
    # prefetch_skipped는 v23에서 관문 자체를 제거했다(과거 기록 해석용으로만 남긴다).
    "rerank_skipped": "검색 단계에서 제외(현재 비활성)",
    "prefetch_skipped": "검색어·스니펫 사전 제외(제거된 관문 · 과거 기록)",
    "quality_failed": "본문 품질 미달 (한국어 아님은 항상 제외)",
    "prefilter_discarded": "제목 단계 제외",
    "sampling_skipped": "날짜·시간 표본 미선택",
    "extraction_failed": "본문 수집 실패",
    "discard": "본문 확인 후 제외",
    "trend_excluded": "이전 버전 LLM 제외",
    "accepted": "2차 수집 성공",
    "duplicate_url": "이미 수집한 링크",
    "budget_exceeded": "fetch 예산 초과",
    "discovery_only": "메타만 사용(본문 수집 안 함)",
    "seed_only": "검색어 재료로만 사용",
    "candidate": "Tavily 미검수 후보",
    "trend_review": "이전 버전 검토 상태",
    "trend_pending": "이전 버전 분류 대기",
    "duplicate": "중복 콘텐츠",
    "supplementary_collected": "보조 링크 수집 완료",
    "discovered": "발견",
    "url_filtered": "URL 단계 제외 (홈페이지·외국어판)",
}
METHOD_LABEL = {
    "board_list": "게시판 목록",
    "rss": "뉴스 RSS",
    "in_body_link": "본문 내부 링크",
}
