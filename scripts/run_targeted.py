#!/usr/bin/env python3
"""2차 gap_filling(semantic targeted) 수집 실행 래퍼. `python scripts/run_targeted.py [-v --dry-run]`

src.main --mode targeted 로 위임한다(인자는 그대로 전달).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src.main import main  # noqa: E402

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "--mode", "targeted", *sys.argv[1:]]
    main()
