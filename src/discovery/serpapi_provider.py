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
        start: int = 0,
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
            # 기본값 10은 scheduler.py의 page_size 기본값과 반드시 같아야 한다 — 다르면
            # scheduler가 "이 페이지가 꽉 찼는지"를 잘못 판단해 불필요한 페이지 요청을 반복한다
            # (Google이 2025-09-14에 num 파라미터를 무력화해서 실제로도 항상 10개만 온다 —
            # configs/providers.yaml의 ponytail 주석 참고).
            "num": max_results or self._config.get("max_results_per_request", 10),
            "start": start,
            "tbs": f"cdr:1,cd_min:{date_from.strftime('%m/%d/%Y')},cd_max:{date_to.strftime('%m/%d/%Y')}",
            "no_cache": self._config.get("no_cache", False),
        }
        # serpapi.Client가 전달받은 dict에 api_key를 삽입하므로 복사본만 넘긴다.
        # 원본 request_params는 DB에 저장되며 비밀값이 절대 들어가면 안 된다.
        response = self._client.search(dict(request_params))

        results = [
            DiscoveredResult(url=item["link"], rank=start + item.get("position", i + 1))
            for i, item in enumerate(response.get("organic_results", []))
            if "link" in item
        ]
        return DiscoveryResponse(
            results=results, request_params=request_params,
            usage=response.get("search_metadata", {}),
        )
