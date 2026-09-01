"""taxonomy 적합성 + 광고/위키/일반정의 제외 필터."""

from __future__ import annotations

from openai import OpenAI

from src.filtering.pipeline import FilterContext, FilterOutcome
from src.utils.prompts import call_structured_output

_CONTENT_PREVIEW_CHARS = 6000


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
    usage = {
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "elapsed_s": result.elapsed_s,
    }
    if not data["fits"]:
        return FilterOutcome(passed=False, reason="taxonomy_mismatch", detail=data["reason"], **usage)
    return FilterOutcome(passed=True, detail=data["reason"], **usage)
