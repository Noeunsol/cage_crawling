"""테스트 전역 설정.

실제 LLM API 호출을 차단한다: 로컬 .env에 유효한 OPENAI_API_KEY가 있으면 run_trend 등이
진짜 API를 호출해 과금·비결정성이 생기고 "키 없음→pending" 가정 테스트가 깨진다.
기본적으로 _complete_json을 오프라인(None)으로 강제하고, 특정 LLM 응답이 필요한 테스트는
본문에서 다시 monkeypatch한다(본문 setattr이 이 autouse 이후에 적용되어 우선한다).
"""
import pytest

from src.common import classify as matcher
from src.phase2 import provider as _provider


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch):
    monkeypatch.setattr(matcher.LLMMatcher, "_complete_json", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _offline_search(monkeypatch):
    """검색 API도 막는다.

    LLM만 막아두면 2차 전략 경로가 source_router 안에서 TavilyProvider·SerpApiProvider를
    직접 만들어 실제 API를 호출한다(테스트에 provider=Mock을 넘겨도 소용없다).
    실측: 전략을 19개로 늘리자 테스트가 14초 → 158초가 되고 크레딧을 썼다.
    실제 호출이 필요한 테스트는 본문에서 다시 monkeypatch한다.
    """
    def _blocked(self, intent):
        raise AssertionError(
            f"테스트가 실제 {type(self).__name__} 검색을 호출했다. "
            "MockTavilyProvider를 쓰거나 source_router.discover를 monkeypatch할 것.")

    monkeypatch.setattr(_provider.TavilyProvider, "search", _blocked)
    monkeypatch.setattr(_provider.SerpApiProvider, "search", _blocked)
