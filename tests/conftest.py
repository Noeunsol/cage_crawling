"""테스트 전역 설정.

실제 LLM API 호출을 차단한다: 로컬 .env에 유효한 OPENAI_API_KEY가 있으면 run_trend 등이
진짜 API를 호출해 과금·비결정성이 생기고 "키 없음→pending" 가정 테스트가 깨진다.
기본적으로 _complete_json을 오프라인(None)으로 강제하고, 특정 LLM 응답이 필요한 테스트는
본문에서 다시 monkeypatch한다(본문 setattr이 이 autouse 이후에 적용되어 우선한다).
"""
import pytest

from src.classify import matcher


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch):
    monkeypatch.setattr(matcher.LLMMatcher, "_complete_json", lambda *a, **k: None)
