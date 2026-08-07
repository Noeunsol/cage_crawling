"""Phase 2 — Search API Router.

Search API는 URL 후보를 '발견'만 한다 (본문 추출은 Extractor 역할, §9).
DiscoveryRouter가 discovery_method(serpapi_site/tavily/...)에 맞는 client를 골라 호출한다.
SerpAPI는 실제 SDK를 사용하고, Tavily는 아직 mock이다.
"""
from __future__ import annotations

import hashlib
import logging
import os
from urllib.parse import urlparse

import serpapi

from ..policy import Subtype
from .query import GeneratedQuery
from ..schema import UrlCandidate
from ..site_registry import SiteRegistry

log = logging.getLogger(__name__)

# 사이트 제한이 없는 쿼리가 퍼질 대표 도메인 풀 (여러 extractor 경로를 태우기 위함)
_GENERIC_DOMAIN_POOL = ["kin.naver.com", "dcinside.com", "news.naver.com"]


class SearchClient:
    """공통 인터페이스. 실제 client(SerpAPI/Tavily/Exa)가 이 시그니처를 따른다."""
    name = "base"

    def search(self, query: GeneratedQuery, taxonomy_lv2: str, subtype: Subtype,
               registry: SiteRegistry) -> list[UrlCandidate]:
        raise NotImplementedError


class _MockClient(SearchClient):
    """결정론적 mock. 쿼리 텍스트 해시로 URL/점수를 만든다."""
    results_per_query = 2

    def search(self, query, taxonomy_lv2, subtype, registry):
        domains = self._target_domains(query, subtype, registry)
        keyword = subtype.keywords[0] if subtype.keywords else subtype.name
        # 스니펫에 subtype 키워드 여러 개를 실어 downstream 매칭이 해당 subtype으로 잡히게 함
        kw_phrase = " ".join(subtype.keywords[:3]) if subtype.keywords else subtype.name
        candidates = []
        for i, domain in enumerate(domains):
            h = _hash(f"{self.name}|{query.text}|{domain}|{i}")
            # 날짜 힌트는 절반만 채워 published_at 누락 경로를 실제로 태운다.
            date_hint = "2026-06-12" if int(h[:2], 16) % 2 == 0 else None
            candidates.append(UrlCandidate(
                source_url=f"https://{domain}/post/{h[:10]}",
                domain=domain,
                search_query=query.text,
                search_api=self.name,
                taxonomy_lv2_candidate=taxonomy_lv2,
                subtype_candidate=subtype.name,
                title=f"[{subtype.name}] {keyword} 관련 게시글 {h[:4]}",
                snippet=f"온라인 커뮤니티에서 {kw_phrase} 관련 논란과 댓글 반응...",
                published_at_hint=date_hint,
                score=0.5 + (int(h[2:4], 16) % 50) / 100.0,
            ))
        return candidates

    def _target_domains(self, query, subtype, registry) -> list[str]:
        if query.site_name:
            d = registry.domain_for(query.site_name)
            return [d] * self.results_per_query if d else []
        return _GENERIC_DOMAIN_POOL[: self.results_per_query + 1]


class SerpApiClient(SearchClient):
    name = "serpapi"

    def search(self, query, taxonomy_lv2, subtype, registry):
        api_key = os.getenv("SERPAPI_KEY")
        if not api_key:
            raise RuntimeError("SERPAPI_KEY 환경변수가 필요합니다")

        results = serpapi.Client(api_key=api_key, timeout=20).search({
            "engine": "google",
            "q": query.text,
            "hl": "ko",
            "gl": "kr",
            "num": 10,
        })
        candidates = []
        for position, item in enumerate(results.get("organic_results", []), start=1):
            url = item.get("link")
            if not url:
                continue
            candidates.append(UrlCandidate(
                source_url=url,
                domain=urlparse(url).netloc.lower(),
                search_query=query.text,
                search_api=self.name,
                taxonomy_lv2_candidate=taxonomy_lv2,
                subtype_candidate=subtype.name,
                title=item.get("title"),
                snippet=item.get("snippet"),
                # "3 days ago" 같은 비정규 날짜는 본문 추출 후 확정한다.
                published_at_hint=None,
                score=max(0.0, 1.0 - (position - 1) * 0.05),
            ))
        return candidates


class MockTavily(_MockClient):
    name = "tavily"


# Exa는 seam만 (AI/보안 문서용, 1차 미구현)
_CLIENTS: dict[str, SearchClient] = {
    "serpapi": SerpApiClient(),
    "tavily": MockTavily(),
}


def get_client(name: str) -> SearchClient | None:
    """미구현(Exa 등)이면 None. 호출측이 fallback을 결정한다."""
    return _CLIENTS.get(name)


def run_client(client: SearchClient, queries, taxonomy_lv2: str, subtype: Subtype,
               registry: SiteRegistry, collection_type: str,
               discovery_method: str | None = None, limit: int | None = None,
               max_queries: int | None = None) -> list[UrlCandidate]:
    """쿼리 목록을 client로 실행하고 collection_type/discovery_method 태그를 붙여 반환."""
    out: list[UrlCandidate] = []
    for q in queries[:max_queries]:
        try:
            results = client.search(q, taxonomy_lv2, subtype, registry)
        except Exception as exc:
            log.warning("search provider 실패 api=%s query=%r: %s", client.name, q.text, exc)
            continue
        for c in results[:limit]:
            c.collection_type = collection_type
            c.discovery_method = discovery_method or client.name
            info = registry.lookup(c.domain)
            c.site_name, c.site_type = info.site_name, info.site_type
            out.append(c)
    return out


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
