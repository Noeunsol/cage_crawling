"""Phase 2 — Search API Router.

Search API는 URL 후보를 '발견'만 한다 (본문 추출은 Extractor 역할, §9).
policy.preferred_search_api 의 primary → fallback 순으로 시도, 결과가 나오면 멈춤.
실제 SDK 호출 없이 결정론적 mock 후보를 생성한다.
"""
from __future__ import annotations

import hashlib

from .policy import Subtype
from .schema import UrlCandidate
from .site_registry import SiteRegistry

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


class MockSerpapi(_MockClient):
    name = "serpapi"


class MockTavily(_MockClient):
    name = "tavily"


# Exa는 seam만 (AI/보안 문서용, 1차 미구현)
_CLIENTS: dict[str, SearchClient] = {
    "serpapi": MockSerpapi(),
    "tavily": MockTavily(),
}


def get_client(name: str) -> SearchClient | None:
    """미구현(Exa 등)이면 None. 호출측이 fallback을 결정한다."""
    return _CLIENTS.get(name)


def run_client(client: SearchClient, queries, taxonomy_lv2: str, subtype: Subtype,
               registry: SiteRegistry, collection_method: str) -> list[UrlCandidate]:
    """쿼리 목록을 client로 실행하고 collection_method 태그를 붙여 반환."""
    out: list[UrlCandidate] = []
    for q in queries:
        for c in client.search(q, taxonomy_lv2, subtype, registry):
            c.collection_method = collection_method
            out.append(c)
    return out


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
