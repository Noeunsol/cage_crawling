"""프로젝트 기본 경로·설정 파일 위치 중앙 관리.

db/report/csv/logs/config 경로를 한 곳에 모은다. 폴더를 재편할 때 여기 상수만 바꾸면
main·phase1·phase2·streamlit 기본값이 함께 따라온다. 하드코딩된 경로 문자열을 새로
만들지 말고 반드시 여기서 import할 것.
경로는 cwd(프로젝트 루트) 기준 상대 문자열 — 기존 코드가 문자열 경로를 그대로 쓰므로 호환.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── 설정(configs/) ──
TAXONOMY_CONFIG = "configs/taxonomy.yaml"
SITE_CONFIG = "configs/site_policy.yaml"
SETTINGS_CONFIG = "configs/crawler_settings.yaml"
TREND_CONFIG = "configs/trend_collection.yaml"
PHASE2_CONFIG = "configs/targeted_collection.yaml"
LLM_CONFIG = "configs/llm.yaml"

# ── 데이터베이스(data/db/) ──
DB_DEFAULT = "data/db/content.db"
PHASE2_DB_DEFAULT = "data/db/phase2_pilot.db"

# ── 내보내기(data/exports/) ──
EXPORT_DIR = "data/exports"
REPORT_DEFAULT = "data/exports/report.json"
CSV_DEFAULT = "data/exports/content.csv"
PHASE2_REPORT_DEFAULT = "data/exports/phase2_report.json"


def review_csv(lv2: str) -> str:
    """수동 표본 검수 CSV 경로 (phase2.review.sample_for_review)."""
    return f"{EXPORT_DIR}/review_{lv2}.csv"


# ── 로그(logs/) ──
LOG_DIR = "logs"
APP_LOG = "logs/app.log"
LLM_USAGE_LOG = "logs/llm_usage.jsonl"
