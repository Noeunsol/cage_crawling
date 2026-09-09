"""OpenAI로 taxonomy 적합성 + 한국 관련성을 0~4점으로 채점해 둘 중 하나라도 임계값 미만이면 제외하는 필터."""

from __future__ import annotations

from openai import OpenAI

from src.filtering.pipeline import FilterContext, FilterOutcome
from src.utils.prompts import call_structured_output

_CONTENT_PREVIEW_CHARS = 6000
_MIN_SCORE = 1  # 이 점수 미만이면 제외 (taxonomy_fit_score/korea_relevance_score 둘 다 0~4점)


def _format_criteria(criteria: list[str]) -> str:
    return "\n".join(f"- {c}" for c in criteria) if criteria else "(지정된 기준 없음)"


def check(ctx: FilterContext, client: OpenAI, prompt_cfg: dict, model: str) -> FilterOutcome:
    result = call_structured_output(
        client, prompt_cfg, model,
        title=ctx.title, content=ctx.content[:_CONTENT_PREVIEW_CHARS],
        type_name=ctx.type_name, definition=ctx.definition,
        include_criteria=_format_criteria(ctx.include_criteria),
        exclude_criteria=_format_criteria(ctx.exclude_criteria),
    )
    data = result.data
    taxonomy_score = data["taxonomy_fit_score"]
    korea_score = data["korea_relevance_score"]
    usage = {
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "elapsed_s": result.elapsed_s,
    }
    detail = f"taxonomy_fit={taxonomy_score}, korea_relevance={korea_score} — {data['reason']}"
    if taxonomy_score < _MIN_SCORE:
        return FilterOutcome(passed=False, reason="taxonomy_mismatch", detail=detail, **usage)
    if korea_score < _MIN_SCORE:
        return FilterOutcome(passed=False, reason="low_korea_relevance", detail=detail, **usage)
    return FilterOutcome(passed=True, detail=detail, **usage)
