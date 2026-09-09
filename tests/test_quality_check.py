"""CSV 사후 품질 체크: quality_* 컬럼이 CSV에 추가되고, 이미 평가된 행은 재호출되지 않는지 확인한다."""

import asyncio
import csv
from unittest.mock import AsyncMock

import pytest

from experiments.quality_check import _eval_row, process_file
from src.utils.prompts import StructuredOutputResult

CONFIGS = {"taxonomy": {"taxonomy": [{"lv2_id": "lv2-test", "types": [
    {"name": "typeA", "definition": "d", "include_criteria": ["a"], "exclude_criteria": ["b"]}
]}]}}
TYPE_INFO = {"definition": "d", "include_criteria": ["a"], "exclude_criteria": ["b"]}


def _fake_result(a: int, k: int, m: int) -> StructuredOutputResult:
    dim = lambda score: {"score": score, "evidence": [], "reason": "r"}
    return StructuredOutputResult(
        data={
            "taxonomy_alignment": dim(a), "korean_context_grounding": dim(k), "context_meaningfulness": dim(m),
            "context_elements": {
                "actor": None, "target": None, "action_or_technique": None,
                "setting_or_service": None, "harm_or_outcome": None, "korean_cues": [],
            },
            "issue_tags": [],
        },
        prompt_tokens=1, completion_tokens=1, elapsed_s=0.01,
    )


def test_eval_row_fills_quality_columns(monkeypatch):
    monkeypatch.setattr(
        "experiments.quality_check.call_structured_output_async",
        AsyncMock(return_value=_fake_result(3, 3, 3)),
    )
    row = {"title": "t", "content": "c"}
    error = asyncio.run(
        _eval_row(None, {}, "model", TYPE_INFO, "typeA", row, asyncio.Semaphore(1))
    )
    assert error is None
    assert row["quality_taxonomy_alignment"] == 3
    assert row["quality_korean_context_grounding"] == 3
    assert row["quality_context_meaningfulness"] == 3
    assert row["quality_issue_tags"] == "[]"


def test_process_file_writes_columns_and_skips_already_scored(tmp_path, monkeypatch):
    lv2_dir = tmp_path / "lv2-test"
    lv2_dir.mkdir()
    path = lv2_dir / "typeA.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["title", "content", "date", "url", "source_domain"])
        writer.writeheader()
        writer.writerow({"title": "t1", "content": "c1", "date": "", "url": "u1", "source_domain": "d1"})

    monkeypatch.setattr("experiments.quality_check.CSV_DIR", tmp_path)
    call = AsyncMock(return_value=_fake_result(3, 2, 2))
    monkeypatch.setattr("experiments.quality_check.call_structured_output_async", call)

    asyncio.run(process_file(None, {}, "model", CONFIGS, path, None, asyncio.Semaphore(1)))

    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["quality_taxonomy_alignment"] == "3"
    assert rows[0]["quality_korean_context_grounding"] == "2"
    assert call.await_count == 1

    # 재실행하면 이미 평가된 행은 다시 호출되지 않는다.
    asyncio.run(process_file(None, {}, "model", CONFIGS, path, None, asyncio.Semaphore(1)))
    assert call.await_count == 1
