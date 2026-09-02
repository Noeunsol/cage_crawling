"""정량 비교 실험이 공유하는 경로."""

from pathlib import Path

from src.config.loader import PROJECT_ROOT

EXPERIMENT_DB_PATH = PROJECT_ROOT / "database" / "test_content.db"
EXPERIMENT_LOG_PATH = Path(__file__).with_name("test_experiment_runs.jsonl")
EXPERIMENT_CSV_DIR = PROJECT_ROOT / "data" / "test"
