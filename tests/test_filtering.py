"""Phase 8 완료 조건 검증: 제거 조건 미해당 콘텐츠만 accepted가 되고, 싼 필터가 먼저 돈다."""

import json
from datetime import date
from types import SimpleNamespace

from src.filtering import blacklist_filter, date_filter, korea_relevance_filter, taxonomy_filter
from src.filtering.pipeline import FilterContext, FilterOutcome, build_filter_chain, run_filters
from src.utils.text import compute_content_hash


class FakeOpenAI:
    def __init__(self, payload: dict):
        self._payload = payload
        self.call_count = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.call_count += 1
        self.last_call = kwargs
        content = json.dumps(self._payload, ensure_ascii=False)
        usage = SimpleNamespace(prompt_tokens=50, completion_tokens=10)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage,
        )


def _ctx(**overrides) -> FilterContext:
    defaults = dict(
        canonical_url="https://example.com/a", source_domain="example.com",
        title="제목", content="본문 내용입니다.", content_hash=compute_content_hash("제목", "본문 내용입니다."),
        published_date="2025-06-01", date_from=date(2025, 1, 1), date_to=date(2026, 1, 1),
        type_name="suicide", definition="자살 방법을 안내하거나 조장하는 행위",
        include_criteria=[], exclude_criteria=[],
    )
    defaults.update(overrides)
    return FilterContext(**defaults)


# ---------------------------------------------------------------- blacklist_filter
def test_blacklist_filter():
    ctx = _ctx(source_domain="www.bad.com")
    assert blacklist_filter.check(ctx, ["bad.com"]).passed is False
    assert blacklist_filter.check(ctx, ["other.com"]).passed is True


# ---------------------------------------------------------------- date_filter
def test_date_filter_within_and_outside_range():
    assert date_filter.check(_ctx(published_date="2025-06-01")).passed is True
    assert date_filter.check(_ctx(published_date="2024-01-01")).passed is False
    assert date_filter.check(_ctx(published_date="2027-01-01")).passed is False


def test_date_filter_passes_when_date_unknown_or_unparseable():
    assert date_filter.check(_ctx(published_date=None)).passed is True
    assert date_filter.check(_ctx(published_date="이상한값")).passed is True



# ---------------------------------------------------------------- LLM 필터
def test_korea_relevance_filter_rejects_when_korean_ratio_too_low():
    # OpenAI 없이 한글 비율로만 판단한다 (사용자 결정, 2026-08-25).
    outcome = korea_relevance_filter.check(_ctx(title="English", content="This is all English content."))
    assert outcome.passed is False
    assert outcome.reason == "low_korea_relevance"


def test_korea_relevance_filter_accepts_when_korean_ratio_high():
    outcome = korea_relevance_filter.check(_ctx(title="제목", content="이것은 충분히 긴 한국어 본문 내용입니다."))
    assert outcome.passed is True


def test_korea_relevance_filter_ratio_threshold_is_configurable():
    ctx = _ctx(title="mixed", content="한글 korean text mixed english words 섞임")
    strict = korea_relevance_filter.check(ctx, min_korean_ratio=0.9)
    lenient = korea_relevance_filter.check(ctx, min_korean_ratio=0.05)
    assert strict.passed is False
    assert lenient.passed is True


def test_taxonomy_filter_rejects_generic_definition():
    fake = FakeOpenAI({"fits": False, "exclusion_type": "generic_no_case", "reason": "구체적 사례 없음"})
    from src.utils.prompts import load_prompt
    outcome = taxonomy_filter.check(_ctx(), fake, load_prompt("taxonomy_filtering"), "gpt-4o-mini")
    assert outcome.passed is False
    assert outcome.reason == "taxonomy_mismatch"


# ---------------------------------------------------------------- run_filters / build_filter_chain
def test_run_filters_short_circuits_on_first_failure():
    calls = []

    def failing(ctx):
        calls.append("failing")
        return FilterOutcome(passed=False, reason="blacklisted_domain")

    def never_called(ctx):
        calls.append("never_called")
        return FilterOutcome(passed=True)

    decision = run_filters(_ctx(), [("failing", failing), ("never_called", never_called)])
    assert decision.status == "excluded"
    assert decision.reason == "blacklisted_domain"
    assert calls == ["failing"]


def test_build_filter_chain_skips_llm_calls_when_blacklist_rejects_first():
    fake = FakeOpenAI({"fits": True, "exclusion_type": "none", "reason": "ok"})

    chain = build_filter_chain(blacklist_domains=["bad.com"], openai_client=fake, model="gpt-4o-mini")
    decision = run_filters(_ctx(source_domain="bad.com"), chain)

    assert decision.status == "excluded"
    assert decision.reason == "blacklisted_domain"
    assert fake.call_count == 0  # 블랙리스트에서 걸러졌으니 OpenAI는 한 번도 호출되지 않아야 한다


def test_build_filter_chain_accepts_when_everything_passes():
    fake = FakeOpenAI({"fits": True, "reason": "ok", "exclusion_type": "none"})

    chain = build_filter_chain(blacklist_domains=[], openai_client=fake, model="gpt-4o-mini")
    decision = run_filters(_ctx(), chain)
    assert "한글 비율" in decision.outcomes["korea_relevance"].detail  # accepted여도 판단 근거가 남는다

    assert decision.status == "accepted"
    assert fake.call_count == 1  # 이제 taxonomy만 OpenAI를 쓴다 (한국 관련성은 규칙 기반)


def test_build_filter_chain_can_disable_taxonomy_filter():
    fake = FakeOpenAI({"fits": False, "exclusion_type": "generic_no_case", "reason": "should not be called"})

    chain = build_filter_chain(
        blacklist_domains=[], openai_client=fake, model="gpt-4o-mini", enable_taxonomy_filter=False,
    )
    decision = run_filters(_ctx(), chain)

    assert decision.status == "accepted"   # taxonomy가 없으니 규칙 기반 필터만 통과하면 채택된다
    assert "taxonomy" not in decision.outcomes
    assert fake.call_count == 0            # OpenAI가 한 번도 호출되지 않는다
