"""LLM 호출 사용량을 append-only JSONL로 남긴다 (DB 컬럼과 별개의 감사·비용 추적).

한 줄 = 한 호출. best-effort: 로깅 실패가 분류 파이프라인을 절대 깨지 않는다.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path

from src.common import paths

log = logging.getLogger(__name__)


def log_llm_usage(path: str = paths.LLM_USAGE_LOG, **fields) -> None:
    """usage 한 건을 JSONL 한 줄로 append. ts 미지정 시 현재 UTC 시각을 채운다."""
    try:
        fields.setdefault("ts", _dt.datetime.now(_dt.timezone.utc).isoformat())
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(fields, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 (로깅 실패는 삼킨다)
        log.debug("llm usage 로깅 실패: %s", exc)


if __name__ == "__main__":
    import tempfile
    fp = Path(tempfile.mkdtemp()) / "u.jsonl"
    log_llm_usage(str(fp), model="gpt-4o-mini", provider="openai", total=123, cost_usd=0.001)
    log_llm_usage(str(fp), model="gpt-4o-mini", provider="openai", total=456)
    lines = fp.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, lines
    rec = json.loads(lines[0])
    assert rec["model"] == "gpt-4o-mini" and "ts" in rec, rec
    log_llm_usage("/nonexistent\0/bad/path.jsonl", x=1)  # never raises
    print("llm_tracker self-check OK")
