"""GPT-4o-mini로 provider별 검색어를 생성한다 (5.1~5.3절).

- Tavily/SerpAPI 요청을 절대 섞지 않는다: provider를 prompt 입력으로 넘기고, 결과도 provider별로 분리해서 돌려준다.
- 기간 표현("2026년", "최근 1년" 등)과 site: 연산자가 섞인 결과는 여기서 걸러낸다.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

from openai import OpenAI

from src.utils.prompts import render_prompt

# 기간을 가리키는 표현 (5.1절: 검색어에 절대 넣지 않는다. 기간 제한은 API 날짜 필터가 담당)
_DATE_EXPRESSION = re.compile(
    r"\d{4}\s*년|\d{1,2}\s*월|최근\s*\d*\s*(년|개월|달)|올해|작년|재작년|지난\s*(달|주|해|년)|이번\s*(년|해|달)"
)
# 도메인 결합(site:)은 discovery 단계 코드가 담당한다 (5.1, 5.3절) — 모델이 만들면 버린다.
_SITE_OPERATOR = re.compile(r"site\s*:", re.IGNORECASE)


@dataclass
class GeneratedQueries:
    accepted: list[str]
    rejected: list[tuple[str, str]]  # (query_text, reason)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_s: float = 0.0


def build_client(providers_cfg: dict) -> OpenAI:
    env_name = providers_cfg["openai"]["api_key_env"]
    api_key = os.environ.get(env_name)
    if not api_key:
        raise RuntimeError(f"{env_name} 환경변수가 설정되지 않았습니다.")
    return OpenAI(api_key=api_key)


def _format_criteria(criteria: list[str]) -> str:
    return "\n".join(f"- {c}" for c in criteria) if criteria else "(지정된 기준 없음)"


_SERPAPI_MAX_WORDS = 3  # ponytail: LLM은 이 규칙을 프롬프트만으로 항상 지키지 않아, 코드로도 강제한다


def _filter_queries(raw_queries: list[str], provider: str = "") -> GeneratedQueries:
    """중복·기간 표현·site: 연산자가 섞인 검색어를 걸러낸다. serpapi는 단어 수 상한도 강제한다."""
    accepted, rejected, seen = [], [], set()
    for q in raw_queries:
        q = q.strip()
        if not q:
            continue
        if q in seen:
            rejected.append((q, "duplicate_within_batch"))
        elif _DATE_EXPRESSION.search(q):
            rejected.append((q, "date_expression"))
        elif _SITE_OPERATOR.search(q):
            rejected.append((q, "site_operator"))
        elif provider == "serpapi" and len(q.split()) > _SERPAPI_MAX_WORDS:
            rejected.append((q, "too_many_words"))
        else:
            seen.add(q)
            accepted.append(q)
    return GeneratedQueries(accepted=accepted, rejected=rejected)


def generate_queries(
    client: OpenAI,
    prompt_cfg: dict,
    *,
    taxonomy_lv2: str,
    type_name: str,
    definition: str,
    search_vocabulary: list[str] | None = None,
    include_criteria: list[str],
    exclude_criteria: list[str],
    provider: str,
    query_count: int,
    model: str,
) -> GeneratedQueries:
    """provider 하나(tavily 또는 serpapi)에 대한 검색어 묶음을 생성한다."""
    system_prompt, user_prompt = render_prompt(
        prompt_cfg,
        taxonomy_lv2=taxonomy_lv2,
        type_name=type_name,
        definition=definition,
        search_vocabulary=_format_criteria(search_vocabulary or []),
        include_criteria=_format_criteria(include_criteria),
        exclude_criteria=_format_criteria(exclude_criteria),
        provider=provider,
        query_count=str(query_count),
    )

    started = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "queries", "schema": prompt_cfg["output_schema"], "strict": True},
        },
    )
    elapsed_s = time.monotonic() - started
    payload = json.loads(response.choices[0].message.content)
    result = _filter_queries(payload["queries"], provider=provider)
    result.prompt_tokens = response.usage.prompt_tokens
    result.completion_tokens = response.usage.completion_tokens
    result.elapsed_s = elapsed_s
    return result
