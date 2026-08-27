"""SerpAPI/Tavily/OpenAI 각각 다른 예외 타입으로 "API 사용량 한도 초과"를 알린다.

호출부가 라이브러리별 예외 타입을 직접 알 필요 없이 classify()만 거치면 되게 한 곳에 모은다.
한도 초과가 아닌 예외는 그대로 원래 흐름(재시도/전체 실패)을 타야 하므로 None을 돌려줄 뿐 삼키지 않는다.
"""

from __future__ import annotations


class QuotaExceededError(Exception):
    """검색/LLM API의 사용량 한도(크레딧)를 초과했을 때. 요청 자체는 정상이었다는 뜻이라 재시도해도 소용없다."""

    def __init__(self, provider: str, original: Exception):
        self.provider = provider
        self.original = original
        super().__init__(f"{provider} API 사용량 한도를 초과했습니다: {original}")


def classify(provider: str, exc: Exception) -> QuotaExceededError | None:
    if provider == "tavily":
        from tavily.errors import UsageLimitExceededError
        if isinstance(exc, UsageLimitExceededError):
            return QuotaExceededError(provider, exc)
    elif provider == "serpapi":
        from serpapi import SerpApiError
        if isinstance(exc, SerpApiError) and getattr(exc, "status_code", None) in (429, 402):
            return QuotaExceededError(provider, exc)
    elif provider == "openai":
        import openai
        if isinstance(exc, openai.RateLimitError):
            return QuotaExceededError(provider, exc)
    return None
