#!/usr/bin/env python3
"""1차 트렌드 수집 실행 래퍼. `python scripts/run_trend.py [-v --after ...]`

src.main --mode trend 로 위임한다(인자는 그대로 전달). CLI 진입점은 src/main.py 하나이며
이 스크립트는 모드별 편의 실행일 뿐이다.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src.main import main  # noqa: E402

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "--mode", "trend", *sys.argv[1:]]
    main()
