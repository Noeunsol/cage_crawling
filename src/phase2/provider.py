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

from src.common.schema import UrlCandidate, canonicalize_url
from src.phase2.intent_builder import CollectionIntent

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
    query_type: str = ""                 # site: 검색으로 바뀐 query에서도 원래 taxonomy type을 보존


class DiscoveryProvider:
    """semantic intent → URL 후보. 실제 provider(Tavily/Exa/…)가 이 시그니처를 따른다."""
    name = "base"

    def search(self, intent: CollectionIntent) -> list[SearchResult]:
        raise NotImplementedError

    def usage_summary(self) -> dict:
        return {"calls": 0, "credits": 0.0, "queries": []}


class TavilyProvider(DiscoveryProvider):
    """Tavily search-for-AI API. lazy import + TAVILY_API_KEY. 키/의존성 없으면 available()=False."""
    name = "tavily"

    def __init__(self, options: dict | None = None):
        self._client = None
        self.options = options or {}
        self._usage_events: list[dict] = []

    def available(self) -> bool:
        from dotenv import load_dotenv
        load_dotenv()
        return bool(os.getenv("TAVILY_API_KEY")) and importlib.util.find_spec("tavily") is not None

    def _get_client(self):
        if self._client is None:
            from dotenv import load_dotenv
            load_dotenv()
            from tavily import TavilyClient  # lazy: 필요할 때만 dep
            self._client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
        return self._client

    def search(self, intent: CollectionIntent) -> list[SearchResult]:
        """intent.queries를 각각 1회 호출하고 URL 기준 dedup해 flat list로 반환한다.

        fan-out을 provider 안에 가둬서 상위 파이프라인은 쿼리 개수를 몰라도 된다.
        """
        out: list[SearchResult] = []
        seen: set[str] = set()
        for query in intent.queries:
            opts = self.options
            request = dict(
                query=query,
                max_results=intent.max_results,
                search_depth=opts.get("search_depth", "basic"),
                chunks_per_source=int(opts.get("chunks_per_source", 1)),
                topic=opts.get("topic", "general"),
                time_range=opts.get("time_range"),
                start_date=opts.get("start_date"),
                end_date=opts.get("end_date"),
                include_answer=False,
                include_raw_content=False,      # 본문은 우리가 추출한다
                exclude_domains=intent.excluded_domains or None,
                # topic=news면 Tavily 뉴스 색인이 영어권 위주라 한국어 검색어에도
                # abcnews·CNN·reuters가 대거 나온다(실측 2026-08-20: 후보 27건 중 18건).
                # country는 순위 가중치일 뿐 필터가 아니므로 도메인 화이트리스트로 건다.
                include_domains=list(opts.get("include_domains") or []) or None,
            )
            # country는 general에서만 지원하는 랭킹 boost다. 한국 관련성 판정은 rerank/acceptance가 한다.
            if intent.korea_relevance_requirement and request["topic"] == "general":
                request["country"] = opts.get("country", "south korea")
            request["include_usage"] = bool(opts.get("include_usage", False))
            request["safe_search"] = bool(opts.get("safe_search", False))
            resp = self._get_client().search(**{k: v for k, v in request.items() if v is not None})
            usage = resp.get("usage") or {}
            self._usage_events.append({
                "query": query, "credits": float(usage.get("credits") or 0),
                "request_id": resp.get("request_id") or "",
            })
            if opts.get("include_usage"):
                log.info("Tavily usage query=%r usage=%s request_id=%s",
                         query, resp.get("usage"), resp.get("request_id"))
            for i, item in enumerate(resp.get("results", []), start=1):
                url = item.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                content = item.get("content")
                out.append(SearchResult(
                    provider=self.name,
                    target_taxonomy_lv2=intent.target_taxonomy_lv2,
                    query_or_intent=query,      # per-result 출처 쿼리 (by_query 집계의 키)
                    query_type=intent.query_types.get(query, ""),
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

    def usage_summary(self) -> dict:
        return {
            "calls": len(self._usage_events),
            "credits": round(sum(event["credits"] for event in self._usage_events), 3),
            "queries": list(self._usage_events),
        }


class SerpApiProvider(DiscoveryProvider):
    """Google SerpAPI의 site: 검색 결과를 2차 공통 후보 형식으로 변환한다."""
    name = "serpapi"

    def __init__(self, options: dict | None = None, rules_by_lv2: dict | None = None):
        self.options = options or {}
        self.rules_by_lv2 = rules_by_lv2 or {}
        self._usage_events: list[dict] = []

    def available(self) -> bool:
        from dotenv import load_dotenv
        load_dotenv()
        return bool(os.getenv("SERPAPI_KEY")) and importlib.util.find_spec("serpapi") is not None

    def _domains(self, intent: CollectionIntent, query: str, query_type: str | None = None) -> list[str]:
        rule = self.rules_by_lv2.get(intent.target_taxonomy_lv2, {})
        query_type = query_type if query_type is not None else intent.query_types.get(query, "")
        domains = (rule.get("domains_by_type", {}).get(query_type)
                   or rule.get("domains", []) or [])
        excluded = set(intent.excluded_domains) | set(self.options.get("excluded_domains", []))
        max_domains = int(rule.get("max_domains_per_query", self.options.get("max_domains_per_query", 2)))
        return [domain for domain in domains if domain not in excluded][
            :max_domains
        ]

    def _append_terms(self, rule: dict, query_type: str) -> str:
        """taxonomy 성격 어휘를 OR 그룹으로 덧붙인다.

        AND로 붙이면 세 어휘를 모두 포함한 문서만 남아 결과가 사실상 0이 된다.
        type별 지정이 있으면 그쪽이 LV2 공통보다 우선한다.
        """
        terms = (rule.get("append_terms_by_type", {}).get(query_type)
                 or rule.get("append_terms") or [])
        return f" ({' OR '.join(terms)})" if terms else ""

    def _search_terms(self, intent: CollectionIntent) -> list[tuple[str, str]]:
        """SerpAPI 전용 키워드 그룹이 있으면 type당 한 번만 확장한다."""
        rule = self.rules_by_lv2.get(intent.target_taxonomy_lv2, {})
        groups_by_type = rule.get("keyword_groups_by_type", {})
        out: list[tuple[str, str]] = []
        expanded_types: set[str] = set()
        for query in intent.queries:
            query_type = intent.query_types.get(query, "")
            extra = self._append_terms(rule, query_type)
            groups = groups_by_type.get(query_type, [])
            if groups:
                if query_type in expanded_types:
                    continue
                expanded_types.add(query_type)
                out.extend((" ".join(group) + extra, query_type) for group in groups)
            else:
                out.append((query + extra, query_type))
        return out

    @staticmethod
    def _request(client, payload: dict) -> dict:
        """SerpAPI 오류에서 URL·API key가 Streamlit 화면으로 새지 않게 한다."""
        try:
            return client.search(payload)
        except Exception as exc:  # SDK가 requests 예외를 그대로 전달한다.
            wrapped = exc.args[0] if exc.args and isinstance(exc.args[0], Exception) else None
            response = getattr(exc, "response", None) or getattr(wrapped, "response", None)
            status = getattr(response, "status_code", None)
            message = str(exc)
            if status in {401, 403} or any(code in message for code in ("401", "403")):
                raise RuntimeError("SerpAPI 인증 실패: SERPAPI_KEY를 재발급한 키로 교체하세요.") from None
            if status == 429 or "429" in message:
                raise RuntimeError("SerpAPI 요청 한도 또는 크레딧이 부족합니다.") from None
            if status == 400 or "400" in message:
                raise RuntimeError("SerpAPI가 검색 요청을 거부했습니다. 검색어와 요청 옵션을 확인하세요.") from None
            if status and status >= 500:
                raise RuntimeError("SerpAPI 서버 오류입니다. 잠시 후 다시 시도하세요.") from None
            raise RuntimeError("SerpAPI 검색 요청에 실패했습니다. 네트워크와 SerpAPI 상태를 확인하세요.") from None

    def search(self, intent: CollectionIntent) -> list[SearchResult]:
        from dotenv import load_dotenv
        load_dotenv()
        import serpapi

        out: list[SearchResult] = []
        seen: set[str] = set()
        rule = self.rules_by_lv2.get(intent.target_taxonomy_lv2, {})
        base = {
            "engine": self.options.get("engine", "google"), "hl": self.options.get("hl", "ko"),
            "gl": self.options.get("gl", "kr"), "location": self.options.get("location", "South Korea"),
            "google_domain": self.options.get("google_domain", "google.co.kr"),
            "num": int(self.options.get("num", intent.max_results)), "start": int(self.options.get("start", 0)),
            "tbs": rule.get("tbs", self.options.get("tbs")), "safe": self.options.get("safe", "off"),
            "filter": self.options.get("filter", 1), "nfpr": self.options.get("nfpr", 1),
        }
        client = serpapi.Client(api_key=os.getenv("SERPAPI_KEY"), timeout=20)
        for query, query_type in self._search_terms(intent):
            domains = self._domains(intent, query, query_type) or [None]
            for domain in domains:
                # 목록·태그 페이지는 개별 원문이 아니므로 URL 후보로 가져오지 않는다.
                # taxonomy별/도메인별 보정은 config에서만 둔다.
                # 여러 도메인을 site: OR 한 줄로 묶어 보낼 때는 domain이 None이라
                # 도메인별 키가 잡히지 않는다. 그때는 "*"(전체 적용) 항목을 쓴다.
                suffix_map = rule.get("query_suffixes_by_domain", {})
                suffixes = suffix_map.get(domain, []) if domain else suffix_map.get("*", [])
                search_query = " ".join(
                    part for part in ([f"site:{domain}"] if domain else []) + [query] + list(suffixes) if part
                )
                response = self._request(client, {k: v for k, v in {**base, "q": search_query}.items() if v is not None})
                self._usage_events.append({"query": search_query, "credits": 1})
                for rank, item in enumerate(response.get("organic_results", []), start=1):
                    url = item.get("link")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    out.append(SearchResult(
                        provider=self.name, target_taxonomy_lv2=intent.target_taxonomy_lv2,
                        query_or_intent=search_query, query_type=query_type,
                        title=item.get("title") or "", url=url,
                        snippet=item.get("snippet"), content_hint=item.get("snippet"),
                        published_at_hint=item.get("date"), provider_score=max(0.0, 1.0 - (rank - 1) * 0.05),
                        rank=rank, raw_provider_payload=item,
                    ))
        return out

    def usage_summary(self) -> dict:
        return {"calls": len(self._usage_events), "credits": len(self._usage_events), "queries": list(self._usage_events)}


class MockTavilyProvider(DiscoveryProvider):
    """키 없이 테스트/오프라인용 결정론적 provider. intent의 include 어휘로 후보를 만든다."""
    name = "tavily"

    _DOMAINS = ("dcinside.com", "pann.nate.com", "kin.naver.com")

    def available(self) -> bool:
        return True

    def search(self, intent: CollectionIntent) -> list[SearchResult]:
        """실제 provider와 동일하게 queries마다 호출하고 URL dedup한다(호출 수를 테스트로 검증 가능)."""
        kw = (intent.include or ["관련"])[0]
        out = []
        for qi, query in enumerate(intent.queries):
            for i in range(min(intent.max_results, len(self._DOMAINS) * 2)):
                domain = self._DOMAINS[i % len(self._DOMAINS)]
                out.append(SearchResult(
                    provider=self.name,
                    target_taxonomy_lv2=intent.target_taxonomy_lv2,
                    query_or_intent=query,
                    query_type=intent.query_types.get(query, ""),
                    title=f"[{kw}] 관련 한국어 게시글 {qi}-{i}",
                    url=f"https://{domain}/post/{intent.target_taxonomy_lv2}-{qi}-{i}",
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
    queries = ["신상털이 피해 고소 질문", "개인정보 유출 박제 피해 호소"]
    intent = CollectionIntent(
        target_taxonomy_lv2="4_I_Privacy_Infringement",
        queries=queries,
        include=["신상털이"], max_results=3,
    )
    results = MockTavilyProvider().search(intent)
    assert results and results[0].content_hint and results[0].provider == "tavily"
    # 쿼리마다 호출되고, 결과에는 어느 쿼리에서 나왔는지가 남는다 (by_query 집계의 근거)
    assert {r.query_or_intent for r in results} == set(queries)
    assert len({r.url for r in results}) == len(results), "URL dedup 실패"
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
