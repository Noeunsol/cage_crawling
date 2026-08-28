"""웹서치로 type별 최근 표현(fresh vocabulary)을 조사한다.

OpenAI Responses API의 web_search 도구를 한 번 호출해서 최근 1년간 실제로 쓰인 표현을 얻는다.
결과를 캐싱하는 것은 이 모듈의 책임이 아니다 — storage/repositories/fresh_vocabulary.py가 담당한다.
"""

from __future__ import annotations

import json
import re

from openai import OpenAI

from src.filtering.text_safety import sanitize_short_terms
from src.utils.prompts import render_prompt

# web_search 도구는 gpt-4o-mini를 지원하지 않는다 (query_generation의 모델과 다름).
# 실측 비교(gpt-4.1-mini vs gpt-5.6-luna, 5개 type) 결과 gpt-5.6-luna가 비용도 더 낮고(도구 호출비가
# $10/1000으로 4.1-mini의 $25/1000보다 낮아 reasoning 토큰 비용을 상쇄함) 실제 커뮤니티 은어를
# 훨씬 잘 찾아냈다 — 4.1-mini는 사전적 동의어 반복에 그쳤다. 속도는 3배가량 느리지만 type당
# 48시간에 1번만 호출되므로 지장 없다.
_MODEL = "gpt-5.6-luna"  # ponytail: 지원 모델이 바뀌면 여기만 갱신

# gpt-5.6-luna는 가끔 출력 표현에 웹 인용 각주를 붙인다 (예: "사이버렉카 ([a.com](url))") — 검색어로
# 못 쓰므로 제거한다.
_CITATION_SUFFIX = re.compile(r"\s*\(\[[^\]]*\]\([^)]*\)\)")


def _strip_citations(terms: list[str]) -> list[str]:
    return [_CITATION_SUFFIX.sub("", t).strip() for t in terms]



def fetch_fresh_vocabulary(
    client: OpenAI,
    prompt_cfg: dict,
    *,
    type_name: str,
    definition: str,
) -> list[str]:
    system_prompt, user_prompt = render_prompt(prompt_cfg, type_name=type_name, definition=definition)
    response = client.responses.create(
        model=_MODEL,
        tools=[{"type": "web_search"}],
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": prompt_cfg["name"],
                "schema": prompt_cfg["output_schema"],
                "strict": True,
            }
        },
    )
    return sanitize_short_terms(_strip_citations(json.loads(response.output_text)["terms"]))
