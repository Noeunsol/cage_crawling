"""제외 필터 체인: 제거 조건 미해당 콘텐츠만 accepted가 된다.

비용이 싼 규칙 기반 필터(블랙리스트/중복/기간)를 먼저 돌리고, LLM 호출이 필요한 필터
(한국 관련성/taxonomy 적합성)는 그 앞 단계를 통과한 것만 호출해서 API 비용을 아낀다.
실제로 언제 어떤 필터를 어떤 순서로 쓸지는 build_filter_chain()에서 조립한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable


@dataclass
class FilterContext:
    canonical_url: str
    source_domain: str
    title: str
    content: str
    content_hash: str
    published_date: str | None      # YYYY-MM-DD 또는 None
    date_from: date
    date_to: date
    type_name: str
    definition: str
    include_criteria: list[str]
    exclude_criteria: list[str]


@dataclass
class FilterOutcome:
    passed: bool
    reason: str | None = None       # retry_policy.yaml의 reason code (실패했을 때만)
    detail: str | None = None       # content_taxonomy_mappings.decision_reason에 넣을 설명
    # OpenAI를 호출한 필터(taxonomy 등)만 채운다 — 비용/시간 추적용.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_s: float = 0.0


@dataclass
class FilterDecision:
    status: str                     # "accepted" 또는 "excluded"
    reason: str | None = None
    detail: str | None = None
    outcomes: dict[str, FilterOutcome] = None  # 실행된 필터 이름 -> 결과 (accepted여도 다 남는다)

    def __post_init__(self):
        if self.outcomes is None:
            self.outcomes = {}


FilterFunc = Callable[[FilterContext], FilterOutcome]
NamedFilter = tuple[str, FilterFunc]


def run_filters(ctx: FilterContext, checks: list[NamedFilter]) -> FilterDecision:
    """checks를 순서대로 돌리다가 처음 실패하는 필터에서 즉시 멈춘다 (불필요한 LLM 호출 방지).

    accepted가 되어도 그때까지 실행된 필터들의 결과(예: 한국 관련성 판단 근거)는 outcomes에 남아서,
    나중에 "왜 채택됐는지"를 결과 화면에서 보여줄 수 있다.
    """
    outcomes: dict[str, FilterOutcome] = {}
    for name, check in checks:
        outcome = check(ctx)
        outcomes[name] = outcome
        if not outcome.passed:
            return FilterDecision(status="excluded", reason=outcome.reason, detail=outcome.detail, outcomes=outcomes)
    return FilterDecision(status="accepted", outcomes=outcomes)


def build_filter_chain(
    *, blacklist_domains, openai_client, model,
    min_korean_ratio: float | None = None, enable_taxonomy_filter: bool = True,
) -> list[NamedFilter]:
    """실제 실행에서 쓸 필터 순서를 조립한다. 규칙 기반(싸다) → LLM(비싸다) 순서를 고정한다.

    한국 관련성은 OpenAI 없이 한글 비율로 판단한다 (사용자 결정, 2026-08-25).
    taxonomy 적합성(OpenAI 호출)은 enable_taxonomy_filter=False로 끌 수 있다 — 끄면 규칙 기반
    필터(블랙리스트/기간/한국 관련성)만 통과해도 accepted가 된다 (사용자 결정, 2026-08-25).

    중복(URL 완전일치·본문 해시·근사중복)은 여기 없다 — collector.py의 _finalize_candidate가
    fetch 직후 먼저 걸러내고 discarded로 기록한다. 예전엔 여기서도 완전일치를 다시 체크했는데,
    그 시점엔 이미 통과가 보장된 상태라 매 후보마다 SELECT 2번을 그냥 버리는 거였다
    (2026-08-31 성능 개선으로 제거 — src/filtering/duplicate_filter.py도 같이 삭제).
    """
    from src.filtering import blacklist_filter, date_filter, korea_relevance_filter, taxonomy_filter
    from src.utils.prompts import load_prompt

    if min_korean_ratio is None:
        min_korean_ratio = korea_relevance_filter.DEFAULT_MIN_KOREAN_RATIO

    chain: list[NamedFilter] = [
        ("blacklist", lambda ctx: blacklist_filter.check(ctx, blacklist_domains)),
        ("date", date_filter.check),
        ("korea_relevance", lambda ctx: korea_relevance_filter.check(ctx, min_korean_ratio)),
    ]
    if enable_taxonomy_filter:
        taxonomy_prompt = load_prompt("taxonomy_filtering")
        chain.append(("taxonomy", lambda ctx: taxonomy_filter.check(ctx, openai_client, taxonomy_prompt, model)))
    return chain
