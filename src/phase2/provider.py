"""Discovery provider 인터페이스 + 표준 SearchResult + Tavily 실구현.

원칙: provider는 URL 후보 discovery 도구일 뿐이다. 결과의 content_hint(snippet/parsed content)는
후보 판단(rerank)·감사용 metadata로만 쓰고, ContentRecord 본문으로 저장하지 않는다. 본문은 오직
기존 ExtractorRouter가 실제 HTML에서 추출한다.
"""
from __future__ import annotations

import logging
import os
import importlib.util
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..schema import UrlCandidate, canonicalize_url
from .intent_builder import CollectionIntent

log = logging.getLogger(__name__)


@dataclass
class SearchResult:
    provider: str
    target_taxonomy_lv2: str
    query_or_intent: str
    title: str
    url: str
    snippet: str | None = None
    content_hint: str | None = None       # 후보 판단용, 본문 저장 금지
    published_at_hint: str | None = None
    provider_score: float | None = None
    rank: int | None = None
    raw_provider_payload: dict = field(default_factory=dict)


class DiscoveryProvider:
    """semantic intent → URL 후보. 실제 provider(Tavily/Exa/…)가 이 시그니처를 따른다."""
    name = "base"

    def search(self, intent: CollectionIntent) -> list[SearchResult]:
        raise NotImplementedError


class TavilyProvider(DiscoveryProvider):
    """Tavily search-for-AI API. lazy import + TAVILY_API_KEY. 키/의존성 없으면 available()=False."""
    name = "tavily"

    def __init__(self):
        self._client = None

    def available(self) -> bool:
        return bool(os.getenv("TAVILY_API_KEY")) and importlib.util.find_spec("tavily") is not None

    def _get_client(self):
        if self._client is None:
            from dotenv import load_dotenv
            load_dotenv()
            from tavily import TavilyClient  # lazy: 필요할 때만 dep
            self._client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
        return self._client

    def search(self, intent: CollectionIntent) -> list[SearchResult]:
        resp = self._get_client().search(
            query=intent.natural_language_query,
            max_results=intent.max_results,
            search_depth="advanced",
            include_domains=intent.preferred_domains or None,
            include_raw_content=False,      # 본문은 우리가 추출한다
        )
        out = []
        for i, item in enumerate(resp.get("results", []), start=1):
            url = item.get("url")
            if not url:
                continue
            content = item.get("content")
            out.append(SearchResult(
                provider=self.name,
                target_taxonomy_lv2=intent.target_taxonomy_lv2,
                query_or_intent=intent.natural_language_query,
                title=item.get("title") or "",
                url=url,
                snippet=content,
                content_hint=content,
                published_at_hint=item.get("published_date"),
                provider_score=item.get("score"),
                rank=i,
                raw_provider_payload=item,
            ))
        return out


class MockTavilyProvider(DiscoveryProvider):
    """키 없이 테스트/오프라인용 결정론적 provider. intent의 include 어휘로 후보를 만든다."""
    name = "tavily"

    def available(self) -> bool:
        return True

    def search(self, intent: CollectionIntent) -> list[SearchResult]:
        domains = intent.preferred_domains or ["dcinside.com", "pann.nate.com", "kin.naver.com"]
        kw = (intent.include or ["관련"])[0]
        out = []
        for i in range(min(intent.max_results, len(domains) * 2)):
            domain = domains[i % len(domains)]
            out.append(SearchResult(
                provider=self.name,
                target_taxonomy_lv2=intent.target_taxonomy_lv2,
                query_or_intent=intent.natural_language_query,
                title=f"[{kw}] 관련 한국어 게시글 {i}",
                url=f"https://{domain}/post/{intent.target_taxonomy_lv2}-{i}",
                snippet=f"{kw} 관련 피해 호소와 커뮤니티 반응...",
                content_hint=f"{kw} 관련 피해 호소와 커뮤니티 반응 상세 내용...",
                provider_score=0.9 - i * 0.05,
                rank=i + 1,
                raw_provider_payload={},
            ))
        return out


def to_candidate(r: SearchResult, registry) -> UrlCandidate:
    """SearchResult → UrlCandidate. content_hint는 discovery 메타로만 싣는다(본문 아님)."""
    domain = urlparse(r.url).netloc.lower()
    info = registry.lookup(domain)
    cand = UrlCandidate(
        source_url=r.url,
        domain=domain,
        search_query=r.query_or_intent,
        search_api=r.provider,
        taxonomy_lv2_candidate=r.target_taxonomy_lv2,
        subtype_candidate="",
        title=r.title,
        snippet=r.snippet,
        published_at_hint=r.published_at_hint,
        canonical_url=canonicalize_url(r.url),
        site_name=info.site_name,
        site_type=info.site_type,
        collection_type="semantic_discovery",
        discovery_method=r.provider,
        discovery_provider=r.provider,
        discovery_query=r.query_or_intent,
        content_hint=r.content_hint,      # candidate 메타. ContentRecord 본문엔 절대 안 감.
        collection_phase=2,
        score=float(r.provider_score or 0.0),
    )
    return cand


if __name__ == "__main__":
    intent = CollectionIntent(
        target_taxonomy_lv2="4_I_Privacy_Infringement",
        natural_language_query="개인정보 유출 피해 호소 한국어 콘텐츠",
        include=["신상털이"], preferred_domains=["pann.nate.com"], max_results=3,
    )
    results = MockTavilyProvider().search(intent)
    assert results and results[0].content_hint and results[0].provider == "tavily"
    # to_candidate: content_hint는 candidate에만, source_url/canonical 설정
    class _Info:  # 최소 registry stub
        site_name, site_type = "nate_pann", "community"
    class _Reg:
        def lookup(self, d):
            return _Info()
    cand = to_candidate(results[0], _Reg())
    assert cand.content_hint and cand.collection_phase == 2 and cand.discovery_provider == "tavily"
    assert cand.canonical_url and not hasattr(cand, "body_text")   # 후보엔 본문 필드 없음
    print("provider self-check OK")
