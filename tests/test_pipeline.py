"""파이프라인: 검색→추출→필터→저장 전체 흐름과 preflight 요약이 올바르게 동작한다."""

import json
from datetime import date
from types import SimpleNamespace

import requests

from src.discovery.base import DiscoveredResult, DiscoveryResponse
from src.discovery.scheduler import ScheduledCandidate
from src.pipeline.collector import process_candidate, run_collection
from src.pipeline.preflight import run_preflight
from src.storage import database
from src.storage.repositories import queries as queries_repo
from src.storage.repositories import runs as runs_repo
from src.utils.text import compute_content_hash

SAMPLE_HTML = """
<html><head><title>메타 제목</title></head>
<body>
<nav>메뉴 메뉴 메뉴</nav>
<article>
<h1>진짜 제목입니다</h1>
<p>2025-06-01</p>
<p>이것은 본문 첫 문단입니다. 충분히 긴 텍스트를 넣어서 trafilatura가 본문으로 인식하게 합니다.
자살 예방과 관련된 상담 내용을 다루는 커뮤니티 게시글 예시입니다. 실제로는 더 긴 내용이 있을 수 있습니다.</p>
<p>본문 두번째 문단입니다. 계속해서 이야기가 이어집니다. 광고나 배너와는 무관한 순수 텍스트입니다.</p>
</article>
</body></html>
"""

EXTRACTION_CFG = {
    "fetch": {"user_agent": "test-agent", "timeout_seconds": 5},
    "min_content_length": {"default": 10},
    "korea_relevance": {"min_korean_ratio": 0.3},
    "taxonomy_filter": {"enabled": True},
}
RETRY_POLICY = {
    "reasons": {
        "timeout": {"retry_mode": "immediate"},
        "temporary_http_error": {"retry_mode": "immediate"},
        "access_denied": {"retry_mode": "never"},
        "not_found": {"retry_mode": "never"},
        "extraction_empty": {"retry_mode": "after_parser_update"},
        "extraction_too_short": {"retry_mode": "immediate"},
        "duplicate": {"retry_mode": "never"},
    }
}
TYPE_CFG = {
    "definition": "자살 방법을 안내하거나 조장하는 행위",
    "include_criteria": [], "exclude_criteria": [],
}


class _FakeResponse:
    def __init__(self, status_code, text="", url="https://example.com/final"):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = {}
        self.apparent_encoding = "utf-8"
        self.encoding = "utf-8"


class RoutingFakeOpenAI:
    """호출된 prompt의 schema 이름에 맞는 payload를 돌려주는 가짜 OpenAI 클라이언트."""

    def __init__(self, responses: dict[str, dict]):
        self._responses = responses
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        name = kwargs["response_format"]["json_schema"]["name"]
        content = json.dumps(self._responses[name], ensure_ascii=False)
        usage = SimpleNamespace(prompt_tokens=50, completion_tokens=10)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage,
        )


ACCEPT_ALL_OPENAI = lambda: RoutingFakeOpenAI({
    "korea_relevance": {"relevant": True, "reason": "한국 커뮤니티 글"},
    "taxonomy_filtering": {"fits": True, "exclusion_type": "none", "reason": "구체적 사례"},
})


def _candidate(query_id, url="https://example.com/a"):
    return ScheduledCandidate(
        lv2_id="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_id=query_id, url=url, rank=1, relevance_score=0.9,
    )


def _common_kwargs(conn, filter_checks):
    return dict(
        run_id="run-1", type_cfg=TYPE_CFG, date_from=date(2025, 1, 1), date_to=date(2026, 1, 1),
        extraction_cfg=EXTRACTION_CFG, retry_policy=RETRY_POLICY, filter_checks=filter_checks,
    )


def _make_conn(tmp_path):
    conn = database.connect(tmp_path / "test.db")
    runs_repo.create_run(conn, "run-1", {})
    return conn


def _seed_query(conn):
    return queries_repo.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_text="자살 상담 후기", status="generated", created_by="user",
    )


# ---------------------------------------------------------------- process_candidate
def test_process_candidate_accepts_clean_content(tmp_path, monkeypatch):
    conn = _make_conn(tmp_path)
    query_id = _seed_query(conn)
    monkeypatch.setattr("requests.get", lambda *a, **kw: _FakeResponse(200, text=SAMPLE_HTML))

    from src.filtering.pipeline import build_filter_chain
    checks = build_filter_chain(blacklist_domains=[], openai_client=ACCEPT_ALL_OPENAI(), model="gpt-4o-mini")

    outcome = process_candidate(conn, _candidate(query_id), **_common_kwargs(conn, checks))

    assert outcome.status == "accepted"
    row = conn.execute("SELECT * FROM contents").fetchone()
    assert row["status"] == "accepted"
    assert row["title"] == "진짜 제목입니다"
    assert conn.execute("SELECT COUNT(*) c FROM content_discoveries").fetchone()["c"] == 1

    mapping = conn.execute("SELECT * FROM content_taxonomy_mappings").fetchone()
    assert "한국 관련성" in mapping["decision_reason"]  # accepted여도 판단 근거가 남아야 결과 화면에서 보여줄 수 있다


def test_process_candidate_excludes_blacklisted_domain(tmp_path, monkeypatch):
    conn = _make_conn(tmp_path)
    query_id = _seed_query(conn)
    monkeypatch.setattr("requests.get", lambda *a, **kw: _FakeResponse(200, text=SAMPLE_HTML))

    from src.filtering.pipeline import build_filter_chain
    checks = build_filter_chain(
        blacklist_domains=["example.com"], openai_client=ACCEPT_ALL_OPENAI(), model="gpt-4o-mini",
    )

    outcome = process_candidate(conn, _candidate(query_id), **_common_kwargs(conn, checks))

    assert outcome.status == "excluded"
    assert outcome.reason == "blacklisted_domain"
    row = conn.execute("SELECT * FROM contents").fetchone()
    assert row["status"] == "excluded"   # 본문은 그대로 저장된다


def test_process_candidate_skips_fetch_entirely_for_blacklisted_domain(tmp_path, monkeypatch):
    # blacklist_domains를 process_candidate에 직접 넘기면 fetch 시도조차 하지 않고 discarded
    # 처리한다 — 유튜브 같은 애초에 못 가져오는 사이트에 요청을 낭비하지 않기 위함 (2026-08-26).
    conn = _make_conn(tmp_path)
    query_id = _seed_query(conn)

    def boom(*a, **kw):
        raise AssertionError("블랙리스트 도메인인데 fetch를 시도했다")

    monkeypatch.setattr("requests.get", boom)

    outcome = process_candidate(
        conn, _candidate(query_id, url="https://m.kin.naver.com/qna/1"),
        **_common_kwargs(conn, []), blacklist_domains=["kin.naver.com"],
    )

    assert outcome.status == "discarded"
    assert outcome.reason == "blacklisted_domain"
    assert conn.execute("SELECT COUNT(*) c FROM contents").fetchone()["c"] == 0


def test_process_candidate_skips_fetch_entirely_for_homepage_url(tmp_path, monkeypatch):
    # 검색 API가 특정 기사 대신 사이트 홈페이지를 결과로 돌려줄 때가 있다(실측: newsis.com,
    # donga.com) — 여러 기사가 섞여 있어 fetch해도 신뢰할 수 없으므로 요청 자체를 안 한다.
    conn = _make_conn(tmp_path)
    query_id = _seed_query(conn)

    def boom(*a, **kw):
        raise AssertionError("홈페이지 URL인데 fetch를 시도했다")

    monkeypatch.setattr("requests.get", boom)

    outcome = process_candidate(
        conn, _candidate(query_id, url="https://www.newsis.com/"), **_common_kwargs(conn, []),
    )

    assert outcome.status == "discarded"
    assert outcome.reason == "homepage_url"
    assert conn.execute("SELECT COUNT(*) c FROM contents").fetchone()["c"] == 0


def test_process_candidate_discards_duplicate_url_without_fetching(tmp_path, monkeypatch):
    conn = _make_conn(tmp_path)
    query_id = _seed_query(conn)

    def fail_if_called(*a, **kw):
        raise AssertionError("중복 URL이면 fetch를 하면 안 됩니다")
    monkeypatch.setattr("requests.get", fail_if_called)

    from src.storage.repositories import contents as contents_repo
    content_id, _ = contents_repo.upsert_content(
        conn, title="t", content="c", published_date=None, canonical_url="https://example.com/a",
        source_name=None, source_domain="example.com", source_category=None,
        status="accepted", content_hash="h",
    )
    conn.execute(
        """INSERT INTO content_taxonomy_mappings
           (content_id, taxonomy_lv2, type_name, decision) VALUES (?, '1_C_Self_Harm', 'suicide', 'accepted')""",
        (content_id,),
    )

    outcome = process_candidate(conn, _candidate(query_id), **_common_kwargs(conn, filter_checks=[]))
    assert outcome.status == "discarded"
    assert outcome.reason == "duplicate"


def test_process_candidate_reuses_same_url_for_another_taxonomy_without_fetching(tmp_path, monkeypatch):
    conn = _make_conn(tmp_path)
    query_id = _seed_query(conn)
    monkeypatch.setattr("requests.get", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("fetch called")))

    from src.storage.repositories import contents as contents_repo
    content_id, _ = contents_repo.upsert_content(
        conn, title="t", content="c", published_date=None, canonical_url="https://example.com/a",
        source_name=None, source_domain="example.com", source_category=None,
        status="accepted", content_hash="h",
    )

    outcome = process_candidate(conn, _candidate(query_id), **_common_kwargs(conn, filter_checks=[]))

    assert outcome.status == "accepted"
    mapping = conn.execute(
        "SELECT * FROM content_taxonomy_mappings WHERE content_id = ?", (content_id,),
    ).fetchone()
    assert (mapping["taxonomy_lv2"], mapping["type_name"]) == ("1_C_Self_Harm", "suicide")


def test_process_candidate_discards_on_fetch_timeout(tmp_path, monkeypatch):
    conn = _make_conn(tmp_path)
    query_id = _seed_query(conn)

    def raise_timeout(*a, **kw):
        raise requests.exceptions.Timeout()
    monkeypatch.setattr("requests.get", raise_timeout)

    outcome = process_candidate(conn, _candidate(query_id), **_common_kwargs(conn, filter_checks=[]))
    assert outcome.status == "discarded"
    assert outcome.reason == "timeout"

    discarded_row = conn.execute("SELECT * FROM discarded_candidates").fetchone()
    assert discarded_row["retryable"] == 1


def test_process_candidate_discards_extraction_failure(tmp_path, monkeypatch):
    conn = _make_conn(tmp_path)
    query_id = _seed_query(conn)
    tiny_html = "<html><head><title>t</title></head><body><p>짧음</p></body></html>"
    monkeypatch.setattr("requests.get", lambda *a, **kw: _FakeResponse(200, text=tiny_html))

    cfg = {**EXTRACTION_CFG, "min_content_length": {"default": 1000}}
    outcome = process_candidate(
        conn, _candidate(query_id), run_id="run-1", type_cfg=TYPE_CFG,
        date_from=date(2025, 1, 1), date_to=date(2026, 1, 1),
        extraction_cfg=cfg, retry_policy=RETRY_POLICY, filter_checks=[],
    )
    assert outcome.status == "discarded"
    assert outcome.reason == "extraction_too_short"


def test_process_candidate_discards_unexpected_exception_instead_of_crashing(tmp_path, monkeypatch):
    # 알려진 예외(FetchError/ExtractionError)가 아닌 어떤 버그가 나도, 이 후보 하나만
    # discarded 처리되고 예외가 run_collection 루프까지 전파되면 안 된다 (2026-08-26 사고 이후 추가).
    conn = _make_conn(tmp_path)
    query_id = _seed_query(conn)

    def boom(*a, **kw):
        raise ValueError("boom")

    monkeypatch.setattr("requests.get", boom)

    outcome = process_candidate(
        conn, _candidate(query_id), run_id="run-1", type_cfg=TYPE_CFG,
        date_from=date(2025, 1, 1), date_to=date(2026, 1, 1),
        extraction_cfg=EXTRACTION_CFG, retry_policy=RETRY_POLICY, filter_checks=[],
    )

    assert outcome.status == "discarded"
    assert outcome.reason == "unexpected_error"
    assert "boom" in outcome.detail


def test_process_candidate_discards_same_content_different_url(tmp_path, monkeypatch):
    conn = _make_conn(tmp_path)
    query_id = _seed_query(conn)
    monkeypatch.setattr("requests.get", lambda *a, **kw: _FakeResponse(200, text=SAMPLE_HTML, url="https://example.com/mirror"))

    from src.extraction.general_extractor import extract
    from src.storage.repositories import contents as contents_repo

    # extractor가 만들어낼 해시와 맞추기 위해 실제로 한 번 뽑아본 뒤 그 해시로 기존 콘텐츠를 만든다.
    pre_extracted = extract(SAMPLE_HTML, "https://example.com/original", 10)
    real_hash = compute_content_hash(pre_extracted.title, pre_extracted.content)

    contents_repo.upsert_content(
        conn, title=pre_extracted.title, content=pre_extracted.content, published_date=None,
        canonical_url="https://example.com/original", source_name=None,
        source_domain="example.com", source_category=None, status="accepted", content_hash=real_hash,
    )

    outcome = process_candidate(
        conn, _candidate(query_id, url="https://example.com/mirror-entry"),
        **_common_kwargs(conn, filter_checks=[]),
    )
    assert outcome.status == "discarded"
    assert outcome.reason == "duplicate"
    assert conn.execute("SELECT COUNT(*) c FROM content_duplicates").fetchone()["c"] == 1


# ---------------------------------------------------------------- run_collection
def test_run_collection_end_to_end(tmp_path, monkeypatch):
    conn = _make_conn(tmp_path)
    queries_repo.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_text="자살 상담 후기", status="generated", created_by="user",
    )
    monkeypatch.setattr("requests.get", lambda *a, **kw: _FakeResponse(200, text=SAMPLE_HTML))

    class FakeTavily:
        def search(self, query_text, **kwargs):
            return DiscoveryResponse(
                results=[DiscoveredResult(url="https://example.com/a", rank=1)],
                request_params={"query": query_text}, usage={"requests": 1},
            )

    configs = {
        "providers": {"openai": {"model": "gpt-4o-mini"}},
        "type_domains": {"types": {}},
        "domain_aliases": {"groups": {}},
        "blacklist": {"domains": []},
        "collection": {"provider_ratio": {"default": {"tavily": 100, "serpapi": 0}}},
        "extraction": EXTRACTION_CFG,
        "retry_policy": RETRY_POLICY,
        "taxonomy": {"taxonomy": [{"lv2_id": "1_C_Self_Harm", "lv2_name": "Self_Harm", "types": [
            {"name": "suicide", **TYPE_CFG},
        ]}]},
    }

    events_seen = []
    summary = run_collection(
        conn, {"tavily": FakeTavily(), "serpapi": None}, configs, "run-1",
        targets=[("1_C_Self_Harm", "suicide")], target_count=1, candidate_multiplier=1.0,
        date_range_by_lv2={"1_C_Self_Harm": (date(2025, 1, 1), date(2026, 1, 1))},
        provider_ratio_by_lv2={}, openai_client=ACCEPT_ALL_OPENAI(),
        on_progress=events_seen.append,
    )

    assert summary.accepted == 1
    assert summary.candidates_found == 1
    assert len(events_seen) == 1
    assert events_seen[0].outcome.status == "accepted"


# ---------------------------------------------------------------- preflight
def test_preflight_reports_missing_domains_and_queries(tmp_path):
    conn = _make_conn(tmp_path)
    queries_repo.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_text="쿼리", status="generated", created_by="user",
    )
    queries_repo.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="serpapi",
        query_text="쿼리2", status="generated", created_by="user",
    )
    configs = {
        "providers": {"openai": {"api_key_env": "OPENAI_API_KEY"},
                       "tavily": {"api_key_env": "TAVILY_API_KEY"},
                       "serpapi": {"api_key_env": "SERPAPI_KEY", "max_pages_per_query": 3}},
        "type_domains": {"types": {}},
        "blacklist": {"domains": []},
    }
    report = run_preflight(
        conn, configs, targets=[("1_C_Self_Harm", "suicide"), ("1_C_Self_Harm", "self_injury")],
        target_count=10, candidate_multiplier=1.5,
    )

    # target_count는 LV2 기준 — 이 LV2의 type 2개(suicide, self_injury)가 10을 나눠 갖는다:
    # ceil(10/2)=5, candidate_target=ceil(5*1.5)=8.
    suicide_report = next(t for t in report.type_reports if t.type_name == "suicide")
    assert suicide_report.candidate_target == 8
    assert suicide_report.tavily_query_count == 1
    assert suicide_report.serpapi_query_count == 1
    assert suicide_report.has_serpapi_domain is False

    assert any("suicide" in w for w in report.domain_missing_warnings)
    assert any("self_injury" in w for w in report.no_query_warnings)
    # tavily 1건 + serpapi 1건 * max_pages_per_query(3) — serpapi는 목표 미달 시 페이지를
    # 넘겨가며 재호출될 수 있으므로 worst-case 추정에 페이지 수를 곱해야 한다.
    assert report.max_requests_estimate == 4


def test_preflight_call_budget_is_shared_across_types_not_multiplied_per_type(tmp_path):
    # type마다 콜 예산을 따로 곱하면(구버전 방식) type 수만큼 worst-case가 부풀려진다 — 같은
    # LV2의 type들은 (lv2, provider) 하나의 예산을 나눠 쓰는 게 scheduler.py의 실제 동작과 맞다.
    conn = _make_conn(tmp_path)
    for type_name in ("suicide", "self_injury"):
        for i in range(5):
            queries_repo.create_query(
                conn, taxonomy_lv2="1_C_Self_Harm", type_name=type_name, provider="serpapi",
                query_text=f"{type_name}쿼리{i}", status="generated", created_by="user",
            )
    configs = {
        "providers": {"openai": {"api_key_env": "OPENAI_API_KEY"},
                       "tavily": {"api_key_env": "TAVILY_API_KEY"},
                       "serpapi": {"api_key_env": "SERPAPI_KEY", "max_results_per_request": 10, "max_pages_per_query": 1}},
        "type_domains": {"types": {}},
        "blacklist": {"domains": []},
        "collection": {"scheduling": {"lane_call_budget_multiplier": 3}},
    }
    report = run_preflight(
        conn, configs, targets=[("1_C_Self_Harm", "suicide"), ("1_C_Self_Harm", "self_injury")],
        target_count=4, candidate_multiplier=1.0,
    )

    # type당 candidate_target=2(합계 4) -> group budget = ceil(4/10)*3 = 3. type마다 따로
    # 3씩(합 6)이 아니라 두 type이 합쳐서 3건만 잡혀야 한다.
    assert report.max_requests_estimate == 3
