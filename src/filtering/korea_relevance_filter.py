"""한국 관련성 필터. OpenAI 없이 한글 비율로 판단한다.

원칙적으로 "한국어로 쓰였는지"와 "한국 관련성"은 다른 개념이다. 다만 이 프로젝트가
검색하는 도메인이 전부 한국 사이트(커뮤니티/법률상담/정부기관 등)라, 본문에 한글이 충분히 섞여
있는지가 실질적으로 괜찮은 대리 지표가 된다 — LLM 판단만큼 정교하진 않지만 API 비용이 없다.

# 한때 "외국 국가명은 있는데 한국 언급 없으면 제외"하는 2차 규칙이 있었으나, 한국어
# 조사(~이란, 명사+"도")가 국가명(이란/인도)과 문자열이 겹쳐 "대법원 판결" 같은 명백한 국내
# 사건까지 오탐 처리했다(2026-08-25). 정교화하는 대신 롤백 — 노이즈가 더 컸다(사용자 결정).
"""

from __future__ import annotations

import re

from src.filtering.pipeline import FilterContext, FilterOutcome

_HANGUL_PATTERN = re.compile(r"[가-힣]")

DEFAULT_MIN_KOREAN_RATIO = 0.3


def check(ctx: FilterContext, min_korean_ratio: float = DEFAULT_MIN_KOREAN_RATIO) -> FilterOutcome:
    text = f"{ctx.title} {ctx.content}".strip()
    if not text:
        return FilterOutcome(passed=False, reason="low_korea_relevance", detail="본문이 비어 있습니다.")

    ratio = len(_HANGUL_PATTERN.findall(text)) / len(text)
    detail = f"한글 비율 {ratio:.0%} (기준 {min_korean_ratio:.0%})"

    if ratio < min_korean_ratio:
        return FilterOutcome(passed=False, reason="low_korea_relevance", detail=detail)
    return FilterOutcome(passed=True, detail=detail)
