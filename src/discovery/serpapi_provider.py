"""SerpAPI discovery provider (5.3절): 짧은 키워드 검색어 + site: 결합으로 검증된 도메인만 검색한다."""

from __future__ import annotations

from datetime import date

from serpapi import Client

from src.discovery.base import DiscoveredResult, DiscoveryResponse


class SerpApiProvider:
    def __init__(self, api_key: str, config: dict):
        self._client = Client(api_key=api_key)
        self._config = config  # configs/providers.yaml의 serpapi 섹션

    def search(
        self,
        query_text: str,
        *,
        date_from: date,
        date_to: date,
        allowed_domains: list[str],
        max_results: int | None = None,
    ) -> DiscoveryResponse:
        """allowed_domains가 비어 있으면 검색하지 않는다 (5.3, 6.3절: 도메인 없으면 SerpAPI 자체를 건너뛴다)."""
        if not allowed_domains:
            raise ValueError(
                "SerpAPI는 type별로 검증된 허용 도메인이 있어야 검색할 수 있습니다 (6.3절). "
                "이 type은 Tavily로만 진행해야 합니다."
            )

        site_filter = " OR ".join(f"site:{d}" for d in allowed_domains)
        request_params = {
            "engine": "google",
            "q": f"({site_filter}) {query_text}",
            "num": max_results or self._config.get("max_results_per_request", 100),
            "tbs": f"cdr:1,cd_min:{date_from.strftime('%m/%d/%Y')},cd_max:{date_to.strftime('%m/%d/%Y')}",
            "no_cache": self._config.get("no_cache", False),
        }
        response = self._client.search(request_params)

        results = [
            DiscoveredResult(url=item["link"], rank=item.get("position", i + 1))
            for i, item in enumerate(response.get("organic_results", []))
            if "link" in item
        ]
        return DiscoveryResponse(
            results=results, request_params=request_params,
            usage=response.get("search_metadata", {}),
        )
