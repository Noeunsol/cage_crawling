"""프로젝트 기본 경로 중앙 관리.

db/report/csv/logs/단계별 파일 경로를 한 곳에 모은다. data/ 폴더를 재편할 때
여기 상수만 바꾸면 main·pipeline·streamlit 기본값이 함께 따라온다.
경로는 cwd(프로젝트 루트) 기준 상대 문자열 — 기존 코드가 문자열 경로를 그대로 쓰므로 호환.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 데이터베이스
DB_DEFAULT = "data/db/content.db"
PHASE2_DB_DEFAULT = "data/db/phase2_pilot.db"

# 내보내기(export)
REPORT_DEFAULT = "data/exports/report.json"
CSV_DEFAULT = "data/exports/content.csv"
PHASE2_REPORT_DEFAULT = "data/exports/phase2_report.json"

# 단계별 파일 저장(ArtifactStore) — content_id별 하위 디렉토리
STAGES_DIR = "data/stages"

# LLM 분류기 설정(crawler_settings에서 분리)
LLM_CONFIG = "configs/llm.yaml"

# 로그
LOG_DIR = "logs"
APP_LOG = "logs/app.log"
LLM_USAGE_LOG = "logs/llm_usage.jsonl"
