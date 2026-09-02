"""configs/logging.yaml로 표준 logging을 초기화한다."""

from __future__ import annotations

import logging.config
from pathlib import Path

from src.config.loader import PROJECT_ROOT


def setup_logging(logging_cfg: dict) -> None:
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    logging.config.dictConfig(logging_cfg)
