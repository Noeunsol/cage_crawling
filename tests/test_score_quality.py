"""2차 검증 게이트: quality_score overall이 기준 미만이면 accepted가 취소되는지 확인한다."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from experiments.score_quality import _MIN_ACCEPTED_OVERALL, _score_one
from src.storage import database
from src.storage.repositories import contents, runs, taxonomy_mappings
from src.utils.prompts import StructuredOutputResult

CONFIGS = {"taxonomy": {"taxonomy": [{"lv2_id": "lv2-test", "types": [{"name": "typeA", "definition": "d"}]}]}}


@pytest.fixture
def conn(tmp_path):
    return database.connect(tmp_path / "test.db")


def _setup_accepted_content(conn, run_id: str) -> int:
    runs.create_run(conn, run_id, {})
    content_id, _ = contents.upsert_content(
        conn, title="t", content="c", published_date=None, canonical_url="https://example.com/1",
        source_name=None, source_domain="example.com", source_category=None,
        status="accepted", content_hash="h1",
    )
    taxonomy_mappings.add_mapping(
        conn, content_id=content_id, taxonomy_lv2="lv2-test", type_name="typeA",
        decision="accepted", decision_reason=None, prompt_name=None, prompt_version=None, model=None,
    )
    return content_id


def _fake_result(overall: int) -> StructuredOutputResult:
    return StructuredOutputResult(
        data={
            "type_relevance": overall, "korean_locality": 3, "specificity": 3,
            "injection_suitability": 3, "overall": overall, "reasoning": "reason", "issues": [],
        },
        prompt_tokens=1, completion_tokens=1, elapsed_s=0.01,
    )


def test_low_overall_cancels_acceptance(conn, monkeypatch):
    content_id = _setup_accepted_content(conn, "run-low")
    monkeypatch.setattr(
        "experiments.score_quality.call_structured_output_async",
        AsyncMock(return_value=_fake_result(_MIN_ACCEPTED_OVERALL - 1)),
    )
    row = {"run_id": "run-low", "content_id": content_id, "type_name": "typeA", "title": "t", "content": "c"}
    error = asyncio.run(_score_one(conn, None, {}, "model", CONFIGS, "lv2-test", row, asyncio.Semaphore(1)))

    assert error is None
    mapping = taxonomy_mappings.list_for_content(conn, content_id)[0]
    assert mapping["decision"] == "excluded"


def test_passing_overall_keeps_acceptance(conn, monkeypatch):
    content_id = _setup_accepted_content(conn, "run-high")
    monkeypatch.setattr(
        "experiments.score_quality.call_structured_output_async",
        AsyncMock(return_value=_fake_result(_MIN_ACCEPTED_OVERALL)),
    )
    row = {"run_id": "run-high", "content_id": content_id, "type_name": "typeA", "title": "t", "content": "c"}
    error = asyncio.run(_score_one(conn, None, {}, "model", CONFIGS, "lv2-test", row, asyncio.Semaphore(1)))

    assert error is None
    mapping = taxonomy_mappings.list_for_content(conn, content_id)[0]
    assert mapping["decision"] == "accepted"
