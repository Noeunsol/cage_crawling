"""Phase 6 완료 조건 검증: round-robin, 목표 달성 시 중단, provider 비율 분배, fingerprint 재사용."""

from datetime import date

from src.discovery.base import DiscoveredResult, DiscoveryResponse, build_fingerprint
from src.discovery.scheduler import run_scheduler
from src.storage import database
from src.storage.repositories import domain_bundles as bundles_repo
from src.storage.repositories import query_executions as exec_repo
from src.storage.repositories import queries as queries_repo
from src.storage.repositories import runs as runs_repo


class FakeProvider:
    """호출될 때마다 미리 정해둔 결과를 하나씩 순서대로 돌려주는 가짜 provider."""

    def __init__(self, responses: list[list[str]]):
        self._responses = list(responses)  # 각 호출마다 반환할 URL 목록
        self.calls: list[tuple[str, dict]] = []

    def search(self, query_text, **kwargs):
        self.calls.append((query_text, kwargs))
        urls = self._responses.pop(0)
        # 실제 provider들처럼 request_params는 JSON 직렬화 가능한 값(문자열)만 담는다.
        serializable_kwargs = {k: str(v) for k, v in kwargs.items()}
        return DiscoveryResponse(
            results=[DiscoveredResult(url=u, rank=i + 1) for i, u in enumerate(urls)],
            request_params={"query": query_text, **serializable_kwargs},
            usage={"requests": 1},
        )


def _make_conn(tmp_path):
    return database.connect(tmp_path / "test.db")


def _seed_query(conn, lv2, type_name, provider, text):
    return queries_repo.create_query(
        conn, taxonomy_lv2=lv2, type_name=type_name, provider=provider,
        query_text=text, status="generated", created_by="user",
    )


BASE_CONFIGS = {
    "type_domains": {"types": {}},   # 기본은 SerpAPI 도메인 없음 -> 전부 Tavily로
    "domain_aliases": {"groups": {}},
    "blacklist": {"domains": []},
    "collection": {"provider_ratio": {"default": {"tavily": 100, "serpapi": 0}}},
}


def test_round_robin_interleaves_types(tmp_path):
    conn = _make_conn(tmp_path)
    runs_repo.create_run(conn, "run-1", {})
    _seed_query(conn, "LV2_A", "type_a", "tavily", "쿼리 A1")
    _seed_query(conn, "LV2_A", "type_a", "tavily", "쿼리 A2")
    _seed_query(conn, "LV2_B", "type_b", "tavily", "쿼리 B1")
    _seed_query(conn, "LV2_B", "type_b", "tavily", "쿼리 B2")

    tavily = FakeProvider([["u1"], ["u2"], ["u3"], ["u4"]])
    date_range = {"LV2_A": (date(2025, 1, 1), date(2026, 1, 1)), "LV2_B": (date(2025, 1, 1), date(2026, 1, 1))}

    run_scheduler(
        conn, {"tavily": tavily, "serpapi": None}, BASE_CONFIGS, "run-1",
        targets=[("LV2_A", "type_a"), ("LV2_B", "type_b")],
        target_count=2, candidate_multiplier=1.0,
        date_range_by_lv2=date_range, provider_ratio_by_lv2={},
    )

    called_queries = [text for text, _ in tavily.calls]
    assert called_queries == ["쿼리 A1", "쿼리 B1", "쿼리 A2", "쿼리 B2"]


def test_stops_issuing_new_requests_once_target_met(tmp_path):
    conn = _make_conn(tmp_path)
    runs_repo.create_run(conn, "run-2", {})
    q1 = _seed_query(conn, "LV2_A", "type_a", "tavily", "쿼리 1")
    _seed_query(conn, "LV2_A", "type_a", "tavily", "쿼리 2")

    tavily = FakeProvider([["u1"], ["u2"]])  # 두 번째는 호출되면 안 됨
    date_range = {"LV2_A": (date(2025, 1, 1), date(2026, 1, 1))}

    result = run_scheduler(
        conn, {"tavily": tavily, "serpapi": None}, BASE_CONFIGS, "run-2",
        targets=[("LV2_A", "type_a")], target_count=1, candidate_multiplier=1.0,
        date_range_by_lv2=date_range, provider_ratio_by_lv2={},
    )

    assert len(tavily.calls) == 1
    assert len(result.candidates) == 1
    assert queries_repo.get_query(conn, q1)["status"] == "used"


def test_missing_serpapi_domains_routes_everything_to_tavily_with_warning(tmp_path):
    conn = _make_conn(tmp_path)
    runs_repo.create_run(conn, "run-3", {})
    _seed_query(conn, "LV2_A", "type_a", "tavily", "쿼리 1")

    tavily = FakeProvider([["u1", "u2"]])
    date_range = {"LV2_A": (date(2025, 1, 1), date(2026, 1, 1))}

    result = run_scheduler(
        conn, {"tavily": tavily, "serpapi": None}, BASE_CONFIGS, "run-3",
        targets=[("LV2_A", "type_a")], target_count=2, candidate_multiplier=1.0,
        date_range_by_lv2=date_range, provider_ratio_by_lv2={"LV2_A": {"tavily": 50, "serpapi": 50}},
    )

    assert any("SerpAPI 허용 도메인이 없어" in w for w in result.warnings)
    assert len(result.candidates) == 2  # 50:50 비율을 무시하고 전량 Tavily로


def test_provider_ratio_splits_target_between_tavily_and_serpapi(tmp_path):
    conn = _make_conn(tmp_path)
    runs_repo.create_run(conn, "run-4", {})
    _seed_query(conn, "LV2_A", "type_a", "tavily", "쿼리 T")
    _seed_query(conn, "LV2_A", "type_a", "serpapi", "쿼리 S")

    tavily = FakeProvider([[f"t{i}" for i in range(7)]])
    serpapi = FakeProvider([[f"s{i}" for i in range(3)]])
    configs = {
        "type_domains": {"types": {"type_a": {"serpapi_allowed_domains": ["example.com"]}}},
        "domain_aliases": {"groups": {}},
        "blacklist": {"domains": []},
        "collection": {"provider_ratio": {"default": {"tavily": 70, "serpapi": 30}}},
    }
    date_range = {"LV2_A": (date(2025, 1, 1), date(2026, 1, 1))}

    result = run_scheduler(
        conn, {"tavily": tavily, "serpapi": serpapi}, configs, "run-4",
        targets=[("LV2_A", "type_a")], target_count=10, candidate_multiplier=1.0,
        date_range_by_lv2=date_range, provider_ratio_by_lv2={},
    )

    by_provider = {"tavily": 0, "serpapi": 0}
    for c in result.candidates:
        by_provider[c.provider] += 1
    assert by_provider == {"tavily": 7, "serpapi": 3}


def test_serpapi_rotates_domain_bundles_within_existing_budget_no_extra_requests(tmp_path):
    conn = _make_conn(tmp_path)
    runs_repo.create_run(conn, "run-6", {})
    _seed_query(conn, "LV2_A", "type_a", "serpapi", "쿼리 1")
    _seed_query(conn, "LV2_A", "type_a", "serpapi", "쿼리 2")

    serpapi = FakeProvider([["s1"], ["s2"]])
    configs = {
        "type_domains": {"types": {"type_a": {
            "serpapi_allowed_domains": ["d1.com", "d2.com", "d3.com", "d4.com"],
        }}},
        "domain_aliases": {"groups": {}},
        "blacklist": {"domains": []},
        "collection": {"provider_ratio": {"default": {"tavily": 0, "serpapi": 100}}},
    }
    date_range = {"LV2_A": (date(2025, 1, 1), date(2026, 1, 1))}

    result = run_scheduler(
        conn, {"tavily": None, "serpapi": serpapi}, configs, "run-6",
        targets=[("LV2_A", "type_a")], target_count=2, candidate_multiplier=1.0,
        date_range_by_lv2=date_range, provider_ratio_by_lv2={},
    )

    # 예산(target=2)만큼만 실제 요청이 나갔고, 로테이션 때문에 추가 요청이 생기지 않았다.
    assert len(serpapi.calls) == 2
    assert len(result.candidates) == 2

    # 두 호출이 서로 다른 도메인 번들(최대 3개씩)을 썼다 — 순환이 실제로 일어났다.
    first_domains = serpapi.calls[0][1]["allowed_domains"]
    second_domains = serpapi.calls[1][1]["allowed_domains"]
    assert first_domains == ["d1.com", "d2.com", "d3.com"]
    assert second_domains == ["d2.com", "d3.com", "d4.com"]

    # 사용한 두 조합만 사용 시각이 기록되고 나머지는 다음 호출 순서를 기다린다.
    rows = bundles_repo.list_bundles(conn, "LV2_A::type_a")
    assert [r["last_used_at"] is not None for r in rows] == [True, True, False, False]


def test_serpapi_fetches_next_pages_only_while_target_is_short(tmp_path):
    conn = _make_conn(tmp_path)
    runs_repo.create_run(conn, "run-pages", {})
    _seed_query(conn, "LV2_A", "type_a", "serpapi", "괴담 확산")
    serpapi = FakeProvider([
        [f"p1-{i}" for i in range(10)],
        [f"p2-{i}" for i in range(10)],
        [f"p3-{i}" for i in range(5)],
    ])
    configs = {
        "type_domains": {"types": {"type_a": {"serpapi_allowed_domains": ["a.com"]}}},
        "domain_aliases": {"groups": {}}, "blacklist": {"domains": []},
        "collection": {"provider_ratio": {"default": {"tavily": 0, "serpapi": 100}}},
        "providers": {"serpapi": {"max_results_per_request": 10, "max_pages_per_query": 3}},
    }

    result = run_scheduler(
        conn, {"tavily": None, "serpapi": serpapi}, configs, "run-pages",
        targets=[("LV2_A", "type_a")], target_count=25, candidate_multiplier=1.0,
        date_range_by_lv2={"LV2_A": (date(2025, 1, 1), date(2026, 1, 1))},
        provider_ratio_by_lv2={},
    )

    assert [call[1]["start"] for call in serpapi.calls] == [0, 10, 20]
    assert len(result.candidates) == 25
    assert len({tuple(call[1]["allowed_domains"]) for call in serpapi.calls}) == 1


def test_serpapi_bundle_fingerprint_differs_per_bundle_so_stale_cache_doesnt_block_rotation(tmp_path):
    """번들이 다르면 fingerprint도 달라서, 한 번들의 캐시가 다른 번들 검색을 막지 않는다."""
    conn = _make_conn(tmp_path)
    runs_repo.create_run(conn, "run-old", {})
    runs_repo.create_run(conn, "run-7", {})
    q1 = _seed_query(conn, "LV2_A", "type_a", "serpapi", "쿼리 1")
    _seed_query(conn, "LV2_A", "type_a", "serpapi", "쿼리 2")

    d_from, d_to = date(2025, 1, 1), date(2026, 1, 1)
    # bundle0(d1~d3)은 이미 "쿼리 1"로 예전에 검색해본 적이 있다고 미리 캐시해둔다.
    fp_bundle0 = build_fingerprint(
        provider="serpapi", query_text="쿼리 1", date_from=d_from, date_to=d_to,
        extra={"domains": ["d1.com", "d2.com", "d3.com"], "start": 0},
    )
    exec_id, _ = exec_repo.start_execution(
        conn, run_id="run-old", query_id=q1, request_params={}, request_fingerprint=fp_bundle0,
    )
    exec_repo.finish_execution(conn, exec_id, status="success", result_count=1)

    serpapi = FakeProvider([["s2"]])
    configs = {
        "type_domains": {"types": {"type_a": {
            "serpapi_allowed_domains": ["d1.com", "d2.com", "d3.com", "d4.com"],
        }}},
        "domain_aliases": {"groups": {}},
        "blacklist": {"domains": []},
        "collection": {"provider_ratio": {"default": {"tavily": 0, "serpapi": 100}}},
    }

    result = run_scheduler(
        conn, {"tavily": None, "serpapi": serpapi}, configs, "run-7",
        targets=[("LV2_A", "type_a")], target_count=2, candidate_multiplier=1.0,
        date_range_by_lv2={"LV2_A": (d_from, d_to)}, provider_ratio_by_lv2={},
    )

    # 1라운드: bundle0을 뽑았는데 캐시가 있어 API를 안 부르고 result_count(1)만 반영.
    # 2라운드: bundle0 다음의 아직 안 쓴 교차 조합(bundle1)이 실제로 호출된다.
    assert len(serpapi.calls) == 1
    assert serpapi.calls[0][1]["allowed_domains"] == ["d2.com", "d3.com", "d4.com"]
    assert len(result.candidates) == 1   # 실제 호출 1건의 결과만 candidate로 반환
    assert result.candidates[0].url == "s2"


def test_already_executed_fingerprint_skips_real_api_call(tmp_path):
    conn = _make_conn(tmp_path)
    runs_repo.create_run(conn, "run-old", {})
    runs_repo.create_run(conn, "run-5", {})
    query_id = _seed_query(conn, "LV2_A", "type_a", "tavily", "쿼리 1")

    d_from, d_to = date(2025, 1, 1), date(2026, 1, 1)
    fingerprint = build_fingerprint(provider="tavily", query_text="쿼리 1", date_from=d_from, date_to=d_to)
    exec_id, _ = exec_repo.start_execution(
        conn, run_id="run-old", query_id=query_id, request_params={}, request_fingerprint=fingerprint,
    )
    exec_repo.finish_execution(conn, exec_id, status="success", result_count=5)

    tavily = FakeProvider([["should-not-be-used"]])
    result = run_scheduler(
        conn, {"tavily": tavily, "serpapi": None}, BASE_CONFIGS, "run-5",
        targets=[("LV2_A", "type_a")], target_count=5, candidate_multiplier=1.0,
        date_range_by_lv2={"LV2_A": (d_from, d_to)}, provider_ratio_by_lv2={},
    )

    assert len(tavily.calls) == 0          # API를 다시 부르지 않았다
    assert result.candidates == []          # 캐시된 실행에는 URL이 없어 새로 만들어내지 않는다


def test_max_calls_by_provider_caps_actual_api_calls(tmp_path):
    conn = _make_conn(tmp_path)
    runs_repo.create_run(conn, "run-6", {})
    _seed_query(conn, "LV2_A", "type_a", "tavily", "쿼리 1")
    _seed_query(conn, "LV2_A", "type_a", "tavily", "쿼리 2")
    _seed_query(conn, "LV2_A", "type_a", "tavily", "쿼리 3")

    tavily = FakeProvider([["u1"], ["u2"], ["u3"]])  # 2번째부터는 호출되면 안 됨
    date_range = {"LV2_A": (date(2025, 1, 1), date(2026, 1, 1))}

    result = run_scheduler(
        conn, {"tavily": tavily, "serpapi": None}, BASE_CONFIGS, "run-6",
        targets=[("LV2_A", "type_a")], target_count=10, candidate_multiplier=1.0,
        date_range_by_lv2=date_range, provider_ratio_by_lv2={},
        max_calls_by_provider={"tavily": 1},
    )

    assert len(tavily.calls) == 1
    assert len(result.candidates) == 1
    assert any("최대 호출 수" in w for w in result.warnings)


class _QuotaExceededProvider:
    """호출되자마자 실제 API처럼 사용량 초과 예외를 던지는 가짜 provider."""

    def __init__(self):
        self.calls = 0

    def search(self, query_text, **kwargs):
        self.calls += 1
        from tavily.errors import UsageLimitExceededError
        raise UsageLimitExceededError("월간 한도 초과")


def test_quota_exceeded_provider_is_skipped_instead_of_crashing(tmp_path):
    conn = _make_conn(tmp_path)
    runs_repo.create_run(conn, "run-7", {})
    _seed_query(conn, "LV2_A", "type_a", "tavily", "쿼리 1")
    _seed_query(conn, "LV2_A", "type_a", "tavily", "쿼리 2")
    date_range = {"LV2_A": (date(2025, 1, 1), date(2026, 1, 1))}

    tavily = _QuotaExceededProvider()
    result = run_scheduler(
        conn, {"tavily": tavily, "serpapi": None}, BASE_CONFIGS, "run-7",
        targets=[("LV2_A", "type_a")], target_count=10, candidate_multiplier=1.0,
        date_range_by_lv2=date_range, provider_ratio_by_lv2={},
    )

    assert tavily.calls == 1               # 첫 실패 이후 이 provider로는 다시 호출하지 않는다
    assert result.candidates == []          # 예외 없이 빈 결과로 정상 반환된다
    assert any("사용량 한도" in w for w in result.warnings)
