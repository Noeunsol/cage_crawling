"""수집 기간 필터 (8.2, 11.2절 3번).

원문 게시일을 못 찾은 경우 어떻게 처리할지는 8.3절 TBD 항목이라, 확정되기 전까지는
안전한 기본값으로 통과시킨다(모르는 걸 근거로 제외하지 않는다). 실제 정책이 정해지면 여기만 고치면 된다.
"""

from __future__ import annotations

from datetime import date as date_cls

from src.filtering.pipeline import FilterContext, FilterOutcome


def check(ctx: FilterContext) -> FilterOutcome:
    if ctx.published_date is None:
        return FilterOutcome(passed=True, detail="게시일을 찾지 못해 기간 필터를 건너뜁니다 (8.3절 TBD).")

    try:
        published = date_cls.fromisoformat(ctx.published_date)
    except ValueError:
        return FilterOutcome(passed=True, detail=f"게시일 형식을 해석할 수 없어 건너뜁니다: {ctx.published_date}")

    if published < ctx.date_from or published > ctx.date_to:
        return FilterOutcome(
            passed=False, reason="date_out_of_range",
            detail=f"게시일 {published}이 요청 기간({ctx.date_from} ~ {ctx.date_to}) 밖입니다.",
        )
    return FilterOutcome(passed=True)
