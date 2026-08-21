"""로깅 설정 한 곳. main.py(CLI)와 streamlit이 동일하게 호출한다.

기존 stderr 출력은 유지하고 logs/app.log(회전)만 추가한다. 두 번 호출해도 핸들러가
중복 등록되지 않도록 idempotent.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.common import paths

_FMT = "%(levelname)s %(name)s: %(message)s"

# 남의 라이브러리가 '정상 실패'를 ERROR로 찍는 것들. 우리 파이프라인이 이미 결과를 기록하므로
# 그대로 두면 진짜 오류가 파묻힌다.
#   trafilatura : 차단 페이지·평문·JSON 응답을 받으면 "not valid HTML"을 ERROR로 찍는다.
#                 추출 실패는 url_candidates.status='extraction_failed'로 이미 남는다.
#   tornado     : 브라우저 탭을 닫거나 새로고침하면 WebSocketClosedError가 뜬다(Streamlit 정상 동작).
_NOISY_LOGGERS = {
    "trafilatura": logging.CRITICAL,
    "tornado.websocket": logging.CRITICAL,
    "tornado.application": logging.CRITICAL,
}


def setup_logging(log_dir: str = paths.LOG_DIR, level: int = logging.WARNING,
                  component: str = "app") -> None:
    root = logging.getLogger()
    root.setLevel(level)

    # stderr 핸들러(기존 basicConfig 동작 보존) — 없을 때만 추가
    if not any(isinstance(h, logging.StreamHandler)
               and not isinstance(h, RotatingFileHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(_FMT))
        root.addHandler(stream)

    # 회전 파일 핸들러 — 같은 파일에 이미 붙어 있으면 재등록 안 함
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    target = Path(log_dir) / "app.log"
    target_resolved = str(target.resolve())
    if not any(isinstance(h, RotatingFileHandler)
               and str(Path(getattr(h, "baseFilename", "")).resolve()) == target_resolved
               for h in root.handlers):
        fileh = RotatingFileHandler(str(target), maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        fileh.setFormatter(logging.Formatter(f"%(asctime)s [{component}] {_FMT}"))
        root.addHandler(fileh)

    for name, noisy_level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(noisy_level)


if __name__ == "__main__":
    import tempfile
    d = tempfile.mkdtemp()
    setup_logging(log_dir=d, level=logging.INFO, component="test")
    setup_logging(log_dir=d, level=logging.INFO, component="test")  # 두 번째 호출
    logging.getLogger("x").info("hello")
    n_file = sum(1 for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler))
    assert n_file == 1, f"파일 핸들러 중복 등록됨: {n_file}"
    assert (Path(d) / "app.log").exists()
    assert logging.getLogger("trafilatura").level == logging.CRITICAL
    print("logging_setup self-check OK")
