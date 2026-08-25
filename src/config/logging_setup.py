"""configs/logging.yaml로 표준 logging을 초기화한다."""

from __future__ import annotations

import logging.config
from pathlib import Path

from src.config.loader import PROJECT_ROOT


def setup_logging(logging_cfg: dict) -> None:
    """logging.yaml 내용으로 logging.config.dictConfig를 적용한다.

    핸들러가 파일에 쓰기 전에 logs/ 디렉터리가 있는지 먼저 만들어준다.
    """
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    logging.config.dictConfig(logging_cfg)
