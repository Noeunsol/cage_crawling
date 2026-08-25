"""Phase 5 완료 조건 검증: provider별 역할과 도메인 분리가 코드로 강제되는지 (mock, 실제 API 호출 없음)."""

from datetime import date

import pytest

from src.discovery.allocator import has_serpapi_domains, serpapi_allowed_domains, tavily_exclude_domains
from src.discovery.base import build_fingerprint
from src.discovery.serpapi_provider import SerpApiProvider
from src.discovery.tavily_provider import TavilyProvider


def test_tavily_exclude_domains_merges_allowed_and_blacklist_dedup():
    type_domains_cfg = {"suicide": {"serpapi_allowed_domains": ["kin.naver.com", "b.com"]}}
    result = tavily_exclude_domains(type_domains_cfg, ["b.com", "youtube.com"], "suicide")
    assert result == ["b.com", "kin.naver.com", "youtube.com"]  # 정렬 + 중복 제거


def test_has_serpapi_domains():
    cfg = {"suicide": {"serpapi_allowed_domains": ["kin.naver.com"]}}
    assert has_serpapi_domains(cfg, "suicide") is True
    assert has_serpapi_domains(cfg, "prompt_attack") is False  # 등록 안 된 type


def test_serpapi_allowed_domains_filters_out_blacklist():
    # type_domains.yaml에 블랙리스트 도메인이 실수로 남아 있어도 실제 검색엔 안 쓰여야 한다.
    cfg = {"rumors": {"serpapi_allowed_domains": ["pann.nate.com", "ppomppu.co.kr"]}}
    assert serpapi_allowed_domains(cfg, "rumors", ["pann.nate.com"]) == ["ppomppu.co.kr"]
    assert has_serpapi_domains(cfg, "rumors", ["pann.nate.com", "ppomppu.co.kr"]) is False


def test_tavily_provider_sends_exclude_domains_and_date_filter(monkeypatch):
    captured = {}

    def fake_search(self, **kwargs):
        captured.update(kwargs)
        return {
            "results": [{"url": "https://a.com/1", "score": 0.9}, {"url": "https://a.com/2"}],
            "usage": {"requests": 1},
        }

    monkeypatch.setattr("tavily.TavilyClient.search", fake_search)
    provider = TavilyProvider(api_key="dummy", config={"search_depth": "basic", "max_results_per_request": 20})

    response = provider.search(
        "자살 생각 커뮤니티 글",
        date_from=date(2025, 1, 1), date_to=date(2026, 1, 1),
        exclude_domains=["youtube.com", "kin.naver.com"],
    )

    assert captured["exclude_domains"] == ["kin.naver.com", "youtube.com"]
    assert captured["start_date"] == "2025-01-01"
    assert captured["end_date"] == "2026-01-01"
    assert "country" not in captured  # config에 country가 없으면 파라미터 자체를 안 보낸다
    assert [r.url for r in response.results] == ["https://a.com/1", "https://a.com/2"]
    assert response.results[0].relevance_score == 0.9
    assert response.usage == {"requests": 1}


def test_tavily_provider_sends_country_when_configured(monkeypatch):
    captured = {}

    def fake_search(self, **kwargs):
        captured.update(kwargs)
        return {"results": [], "usage": {}}

    monkeypatch.setattr("tavily.TavilyClient.search", fake_search)
    provider = TavilyProvider(api_key="dummy", config={"country": "south korea"})

    provider.search(
        "자살 생각 커뮤니티 글", date_from=date(2025, 1, 1), date_to=date(2026, 1, 1), exclude_domains=[],
    )

    assert captured["country"] == "south korea"


def test_serpapi_provider_combines_site_filter_with_query(monkeypatch):
    captured = {}

    def fake_search(self, params):
        captured.update(params)
        return {
            "organic_results": [{"link": "https://kin.naver.com/1", "position": 1}],
            "search_metadata": {"id": "abc"},
        }

    monkeypatch.setattr("serpapi.Client.search", fake_search)
    provider = SerpApiProvider(api_key="dummy", config={"max_results_per_request": 100})

    response = provider.search(
        "자살 상담 후기",
        date_from=date(2025, 1, 1), date_to=date(2026, 1, 1),
        allowed_domains=["kin.naver.com", "pann.nate.com"],
    )

    assert captured["q"] == "(site:kin.naver.com OR site:pann.nate.com) 자살 상담 후기"
    assert "cd_min:01/01/2025" in captured["tbs"]
    assert [r.url for r in response.results] == ["https://kin.naver.com/1"]


def test_serpapi_provider_requires_allowed_domains():
    provider = SerpApiProvider(api_key="dummy", config={})
    with pytest.raises(ValueError):
        provider.search(
            "질문", date_from=date(2025, 1, 1), date_to=date(2026, 1, 1), allowed_domains=[],
        )


def test_fingerprint_is_stable_and_sensitive_to_query_and_date():
    common = dict(provider="tavily", date_from=date(2025, 1, 1), date_to=date(2026, 1, 1))

    fp1 = build_fingerprint(query_text="자살 예방 상담", **common)
    fp1_again = build_fingerprint(query_text="자살 예방 상담", **common)
    fp_diff_query = build_fingerprint(query_text="다른 검색어", **common)
    fp_diff_date = build_fingerprint(
        provider="tavily", query_text="자살 예방 상담",
        date_from=date(2024, 1, 1), date_to=date(2026, 1, 1),
    )

    assert fp1 == fp1_again
    assert fp1 != fp_diff_query
    assert fp1 != fp_diff_date
