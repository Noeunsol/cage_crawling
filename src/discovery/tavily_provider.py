"""Tavily discovery provider: 자연어 검색어로 뉴스·사례·커뮤니티 글을 폭넓게 찾는다."""

from __future__ import annotations

from datetime import date

from tavily import TavilyClient

from src.discovery.base import DiscoveredResult, DiscoveryResponse


class TavilyProvider:
    def __init__(self, api_key: str, config: dict):
        self._client = TavilyClient(api_key=api_key)
        self._config = config  # configs/providers.yaml의 tavily 섹션

    def search(
        self,
        query_text: str,
        *,
        date_from: date,
        date_to: date,
        exclude_domains: list[str],
        max_results: int | None = None,
        topic: str = "general",
    ) -> DiscoveryResponse:
        """설정에서 계산한 제외 도메인(기본값: 공통 블랙리스트)으로 검색한다.

        topic="news"는 뉴스 소스만 인덱싱해서, 위키·학술·법무법인 블로그처럼 게시일이 불분명한
        일반 웹페이지가 날짜 필터를 우회해 들어오는 걸 줄인다 (taxonomy.yaml의 type별 tavily.topic).
        """
        request_params = {
            "query": query_text,
            "search_depth": self._config.get("search_depth", "basic"),
            "topic": topic,
            "start_date": date_from.isoformat(),
            "end_date": date_to.isoformat(),
            "exclude_domains": sorted(exclude_domains),
            "max_results": max_results or self._config.get("max_results_per_request", 20),
            "include_usage": True,
        }
        if self._config.get("country"):
            # 한국 관련 결과로 편향시켜 뒤 단계(fetch/LLM 필터)에서 버려질 해외 결과를 줄인다.
            request_params["country"] = self._config["country"]
        response = self._client.search(**request_params)

        results = [
            DiscoveredResult(url=item["url"], rank=i + 1, relevance_score=item.get("score"))
            for i, item in enumerate(response.get("results", []))
        ]
        return DiscoveryResponse(
            results=results, request_params=request_params, usage=response.get("usage", {}),
        )
