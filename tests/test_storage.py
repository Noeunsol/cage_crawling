"""Phase 2 완료 조건 검증: 성공/중복/실패 이력이 손실 없이 쌓이고, 같은 URL/쿼리는 재저장되지 않는다."""

import pytest

from src.storage import database
from src.storage.repositories import (
    contents, discarded, discoveries, duplicates, queries,
    query_executions, query_generation_calls, runs, taxonomy_mappings,
)


@pytest.fixture
def conn(tmp_path):
    return database.connect(tmp_path / "test.db")


def test_schema_init_is_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    database.connect(db_path)
    second = database.connect(db_path)  # 두 번째 연결도 에러 없이 성공해야 한다
    assert second is not None


def test_run_lifecycle(conn):
    runs.create_run(conn, "run-1", {"target_count": 50})
    row = runs.get_run(conn, "run-1")
    assert row["status"] == "running"

    runs.finish_run(conn, "run-1", "completed", {"tavily": 10}, ["도메인 누락: suicide"])
    row = runs.get_run(conn, "run-1")
    assert row["status"] == "completed"
    assert row["finished_at"] is not None


def test_upsert_content_prevents_duplicate_url(conn):
    kwargs = dict(
        title="제목", content="본문", published_date="2026-01-01",
        canonical_url="https://example.com/a", source_name="예시",
        source_domain="example.com", source_category="news",
        status="accepted", content_hash="hash-a",
    )
    id1, created1 = contents.upsert_content(conn, **kwargs)
    id2, created2 = contents.upsert_content(conn, **kwargs)

    assert created1 is True
    assert created2 is False
    assert id1 == id2
    assert len(conn.execute("SELECT * FROM contents").fetchall()) == 1


def test_taxonomy_mapping_is_idempotent(conn):
    content_id, _ = contents.upsert_content(
        conn, title="t", content="c", published_date=None,
        canonical_url="https://example.com/b", source_name=None,
        source_domain="example.com", source_category=None,
        status="accepted", content_hash="hash-b",
    )
    mapping_kwargs = dict(
        content_id=content_id, taxonomy_lv2="1_C_Self_Harm", type_name="suicide",
        decision="accepted", decision_reason="포함 기준 충족",
        prompt_name="taxonomy_filtering", prompt_version="1", model="gpt-4o-mini",
    )
    taxonomy_mappings.add_mapping(conn, **mapping_kwargs)
    taxonomy_mappings.add_mapping(conn, **mapping_kwargs)  # 재실행 시 중복 저장되면 안 됨

    assert len(taxonomy_mappings.list_for_content(conn, content_id)) == 1


def test_query_creation_dedups_same_text(conn):
    id1 = queries.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_text="자살 방법 커뮤니티 글", status="generated", created_by="openai",
    )
    id2 = queries.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_text="자살 방법 커뮤니티 글", status="generated", created_by="openai",
    )
    assert id1 == id2


def test_query_execution_fingerprint_blocks_reexecution(conn):
    runs.create_run(conn, "run-2", {})
    query_id = queries.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_text="자살 예방 상담", status="generated", created_by="openai",
    )
    exec_id1, already1 = query_executions.start_execution(
        conn, run_id="run-2", query_id=query_id,
        request_params={"q": "자살 예방 상담"}, request_fingerprint="fp-1",
    )
    exec_id2, already2 = query_executions.start_execution(
        conn, run_id="run-2", query_id=query_id,
        request_params={"q": "자살 예방 상담"}, request_fingerprint="fp-1",
    )

    assert already1 is False
    assert already2 is True  # 같은 fingerprint면 재호출하지 말라는 신호
    assert exec_id1 == exec_id2

    query_executions.finish_execution(conn, exec_id1, status="success", result_count=8)
    row = query_executions.get_by_fingerprint(conn, "fp-1")
    assert row["result_count"] == 8


def test_query_executions_count_by_provider_excludes_cached_reuse(conn):
    runs.create_run(conn, "run-count", {})
    tavily_query = queries.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_text="쿼리1", status="generated", created_by="openai",
    )
    serpapi_query = queries.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="serpapi",
        query_text="쿼리2", status="generated", created_by="openai",
    )
    query_executions.start_execution(
        conn, run_id="run-count", query_id=tavily_query, request_params={}, request_fingerprint="fp-a",
    )
    query_executions.start_execution(
        conn, run_id="run-count", query_id=serpapi_query, request_params={}, request_fingerprint="fp-b",
    )
    # 같은 fingerprint로 다시 부르면 캐시 재사용이라 새 행이 안 생긴다 (호출 수에 안 잡혀야 함)
    query_executions.start_execution(
        conn, run_id="run-count", query_id=tavily_query, request_params={}, request_fingerprint="fp-a",
    )

    counts = query_executions.count_by_provider(conn)
    assert counts == {"tavily": 1, "serpapi": 1}


def test_query_generation_calls_sum_usage(conn):
    query_generation_calls.record_call(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily", model="gpt-4o-mini",
        prompt_tokens=100, completion_tokens=20, elapsed_s=1.5,
    )
    query_generation_calls.record_call(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="serpapi", model="gpt-4o-mini",
        prompt_tokens=50, completion_tokens=10, elapsed_s=0.5,
    )

    total = query_generation_calls.sum_usage(conn)

    assert total.calls == 2
    assert total.prompt_tokens == 150
    assert total.completion_tokens == 30
    assert total.elapsed_s == 2.0


def test_sum_taxonomy_filter_openai_usage_reads_provider_usage_summary(conn):
    runs.create_run(conn, "run-tf", {})
    runs.finish_run(
        conn, "run-tf", "completed",
        {"openai": [{"prompt_tokens": 200, "completion_tokens": 40, "elapsed_s": 2.0}], "tavily": [{}]},
        [],
    )

    usage = runs.sum_taxonomy_filter_openai_usage(conn)

    assert usage == {"calls": 1, "prompt_tokens": 200, "completion_tokens": 40, "elapsed_s": 2.0}


def test_discovery_and_discarded_and_duplicate_records(conn):
    runs.create_run(conn, "run-3", {})
    query_id = queries.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="serpapi",
        query_text="site:kin.naver.com 자살", status="generated", created_by="openai",
    )
    content_id, _ = contents.upsert_content(
        conn, title="t", content="c", published_date=None,
        canonical_url="https://example.com/c", source_name=None,
        source_domain="example.com", source_category="qna",
        status="accepted", content_hash="hash-c",
    )

    discoveries.record_discovery(
        conn, content_id=content_id, run_id="run-3", query_id=query_id,
        provider="serpapi", returned_url="https://example.com/c", rank=1,
    )
    assert len(discoveries.list_for_content(conn, content_id)) == 1

    discarded.record_discarded(
        conn, original_url="https://example.com/dead-link",
        normalized_url="https://example.com/dead-link",
        run_id="run-3", query_id=query_id, source_domain="example.com",
        reason="timeout", retryable=True,
    )
    assert len(discarded.list_retryable(conn, "run-3")) == 1

    duplicates.record_duplicate(
        conn, representative_content_id=content_id,
        duplicate_url="https://m.example.com/c", duplicate_reason="same_event",
    )
    assert len(duplicates.list_for_representative(conn, content_id)) == 1


def test_discoveries_list_by_run_joins_lv2_type_and_decision(conn):
    runs.create_run(conn, "run-9", {})
    query_id = queries.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_text="자살 상담 후기", status="used", created_by="user",
    )
    content_id, _ = contents.upsert_content(
        conn, title="제목", content="본문", published_date="2026-01-01",
        canonical_url="https://example.com/a", source_name=None,
        source_domain="example.com", source_category=None,
        status="accepted", content_hash="hash-a",
    )
    taxonomy_mappings.add_mapping(
        conn, content_id=content_id, taxonomy_lv2="1_C_Self_Harm", type_name="suicide",
        decision="accepted", decision_reason="accepted | 한국 관련성: 한국 커뮤니티 글",
        prompt_name=None, prompt_version=None, model=None,
    )
    discoveries.record_discovery(
        conn, content_id=content_id, run_id="run-9", query_id=query_id,
        provider="tavily", returned_url="https://example.com/a", rank=1,
    )

    rows = discoveries.list_by_run(conn, "run-9")

    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "제목"
    assert row["taxonomy_lv2"] == "1_C_Self_Harm"
    assert row["type_name"] == "suicide"
    assert row["decision"] == "accepted"
    assert "한국 관련성" in row["decision_reason"]
    assert row["source_domain"] == "example.com"


def test_taxonomy_mappings_list_with_content_filters_and_includes_provider(conn):
    cid1, _ = contents.upsert_content(
        conn, title="글1", content="본문1", published_date=None, canonical_url="https://a.com/1",
        source_name=None, source_domain="a.com", source_category=None,
        status="accepted", content_hash="h1",
    )
    cid2, _ = contents.upsert_content(
        conn, title="글2", content="본문2", published_date=None, canonical_url="https://a.com/2",
        source_name=None, source_domain="a.com", source_category=None,
        status="excluded", content_hash="h2",
    )
    taxonomy_mappings.add_mapping(
        conn, content_id=cid1, taxonomy_lv2="1_C_Self_Harm", type_name="suicide",
        decision="accepted", decision_reason="accepted", prompt_name=None, prompt_version=None, model=None,
    )
    taxonomy_mappings.add_mapping(
        conn, content_id=cid2, taxonomy_lv2="1_C_Self_Harm", type_name="self_injury",
        decision="excluded", decision_reason="taxonomy_mismatch: ...", prompt_name=None, prompt_version=None, model=None,
    )
    runs.create_run(conn, "run-1", {})
    query_id = queries.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="serpapi",
        query_text="q", status="used", created_by="user",
    )
    discoveries.record_discovery(
        conn, content_id=cid1, run_id="run-1", query_id=query_id, provider="serpapi",
        returned_url="https://a.com/1", rank=1,
    )

    all_rows = taxonomy_mappings.list_with_content(conn)
    assert len(all_rows) == 2

    accepted_only = taxonomy_mappings.list_with_content(conn, decision="accepted")
    assert len(accepted_only) == 1
    assert accepted_only[0]["title"] == "글1"
    assert accepted_only[0]["provider"] == "serpapi"

    by_type = taxonomy_mappings.list_with_content(conn, type_name="self_injury")
    assert len(by_type) == 1
    assert by_type[0]["title"] == "글2"
    assert by_type[0]["provider"] is None  # discovery 기록이 없는 콘텐츠
