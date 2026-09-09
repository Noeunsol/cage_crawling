"""taxonomy_mismatch로 제외된 콘텐츠를 같은 LV2의 형제 type에 재분류해본다.

검색 단계가 엉뚱한 type의 검색어로 콘텐츠를 잘못 데려오는 경우가 있다(2026-09-08 실측,
race_and_ethnicity 검색어로 찾은 콘텐츠가 실제로는 age 판례였음). 본문 자체는 쓸모 있는데
그 type엔 안 맞아서 버려지는 걸 막는다 — low_korea_relevance(언어/지역 문제)에는 적용하지
않는다, type을 바꿔도 그 문제는 그대로다.
"""

from __future__ import annotations

from dataclasses import dataclass

from openai import OpenAI

from src.utils.prompts import call_structured_output

_CONTENT_PREVIEW_CHARS = 6000
_MIN_FIT_SCORE = 3  # 재분류는 신중하게 — openai_filter 1차 판정 기준(1)보다 높게 잡는다


@dataclass
class ReclassifyResult:
    better_type: str | None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_s: float = 0.0


def _format_candidates(sibling_types: list[dict]) -> str:
    return "\n".join(f"- {t['name']}: {t['definition']}" for t in sibling_types)


def find_better_type(
    client: OpenAI, prompt_cfg: dict, model: str, *,
    title: str, content: str, original_type_name: str, sibling_types: list[dict],
) -> ReclassifyResult:
    """sibling_types: [{"name": ..., "definition": ...}, ...] (원래 type 제외, enabled만).

    fit_score가 _MIN_FIT_SCORE 이상이고 목록에 실제 있는 이름을 답했을 때만 better_type을 채운다
    — 목록에 없는 이름을 지어내 답해도(hallucination) 조용히 None 처리한다.
    """
    if not sibling_types:
        return ReclassifyResult(better_type=None)

    result = call_structured_output(
        client, prompt_cfg, model,
        title=title, content=content[:_CONTENT_PREVIEW_CHARS],
        original_type_name=original_type_name, candidate_types=_format_candidates(sibling_types),
    )
    data = result.data
    valid_names = {t["name"] for t in sibling_types}
    better_type = data["best_type"] if data["fit_score"] >= _MIN_FIT_SCORE and data["best_type"] in valid_names else None
    return ReclassifyResult(
        better_type=better_type,
        prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
        elapsed_s=result.elapsed_s,
    )
