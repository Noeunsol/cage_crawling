"""GPT-4o-mini로 provider별 검색어를 생성한다 (5.1~5.3절).

- Tavily/SerpAPI 요청을 절대 섞지 않는다: provider를 prompt 입력으로 넘기고, 결과도 provider별로 분리해서 돌려준다.
- 기간 표현("2026년", "최근 1년" 등)과 site: 연산자가 섞인 결과는 여기서 걸러낸다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from openai import AsyncOpenAI, OpenAI

from src.utils.prompts import call_structured_output, call_structured_output_async

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


def _resolve_api_key(providers_cfg: dict) -> str:
    env_name = providers_cfg["openai"]["api_key_env"]
    api_key = os.environ.get(env_name)
    if not api_key:
        raise RuntimeError(f"{env_name} 환경변수가 설정되지 않았습니다.")
    return api_key


def build_client(providers_cfg: dict) -> OpenAI:
    return OpenAI(api_key=_resolve_api_key(providers_cfg))


def build_async_client(providers_cfg: dict) -> AsyncOpenAI:
    """provider 하나의 tavily/serpapi 호출을 asyncio.gather로 동시에 보낼 때 쓴다."""
    return AsyncOpenAI(api_key=_resolve_api_key(providers_cfg))


def _format_criteria(criteria: list[str]) -> str:
    return "\n".join(f"- {c}" for c in criteria) if criteria else "(지정된 기준 없음)"


# providers.yaml에 query_limits가 없을 때 쓰는 하위 호환 기본값 (기존 serpapi 3단어 상한과 동일).
_DEFAULT_QUERY_LIMITS = {
    "tavily": {"max_terms": None, "max_characters": None},
    "serpapi": {"max_terms": 3, "max_characters": None},
}
# 검색어에 연결어가 토큰 하나를 그냥 차지하는 경우 핵심어 수에서 빼준다 (조사는 띄어쓰기 없이
# 명사에 붙어서 별도 토큰이 되지 않으므로 따로 처리할 필요가 없다).
_STOPWORDS = {"그리고", "또는", "및", "혹은"}


def _content_word_count(q: str) -> int:
    return len([w for w in q.split() if w not in _STOPWORDS])


def _filter_queries(raw_queries: list[str], provider: str = "", limits: dict | None = None) -> GeneratedQueries:
    """중복·기간 표현·site: 연산자가 섞인 검색어를 걸러낸다. provider별 단어수/글자수 상한도 강제한다."""
    provider_limits = (limits or _DEFAULT_QUERY_LIMITS).get(provider, {})
    max_terms = provider_limits.get("max_terms")
    max_characters = provider_limits.get("max_characters")

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
        elif max_terms and _content_word_count(q) > max_terms:
            rejected.append((q, "too_many_words"))
        elif max_characters and len(q) > max_characters:
            rejected.append((q, "too_long"))
        else:
            seen.add(q)
            accepted.append(q)
    return GeneratedQueries(accepted=accepted, rejected=rejected)


def _render_inputs(
    *,
    taxonomy_lv2: str,
    type_name: str,
    definition: str,
    search_vocabulary: list[str] | None,
    include_criteria: list[str],
    exclude_criteria: list[str],
    query_axes: list[str] | None,
    provider: str,
    query_count: int,
) -> dict:
    return dict(
        taxonomy_lv2=taxonomy_lv2,
        type_name=type_name,
        definition=definition,
        search_vocabulary=_format_criteria(search_vocabulary or []),
        include_criteria=_format_criteria(include_criteria),
        exclude_criteria=_format_criteria(exclude_criteria),
        query_axes=_format_criteria(query_axes or []),
        provider=provider,
        query_count=str(query_count),
    )


def _to_generated_queries(output, *, provider: str, query_limits: dict | None) -> GeneratedQueries:
    result = _filter_queries(output.data["queries"], provider=provider, limits=query_limits)
    result.prompt_tokens = output.prompt_tokens
    result.completion_tokens = output.completion_tokens
    result.elapsed_s = output.elapsed_s
    return result


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
    query_axes: list[str] | None = None,
    provider: str,
    query_count: int,
    model: str,
    query_limits: dict | None = None,
) -> GeneratedQueries:
    """provider 하나(tavily 또는 serpapi)에 대한 검색어 묶음을 생성한다."""
    inputs = _render_inputs(
        taxonomy_lv2=taxonomy_lv2, type_name=type_name, definition=definition,
        search_vocabulary=search_vocabulary, include_criteria=include_criteria,
        exclude_criteria=exclude_criteria, query_axes=query_axes, provider=provider, query_count=query_count,
    )
    output = call_structured_output(client, prompt_cfg, model, **inputs)
    return _to_generated_queries(output, provider=provider, query_limits=query_limits)


async def generate_queries_async(
    client: AsyncOpenAI,
    prompt_cfg: dict,
    *,
    taxonomy_lv2: str,
    type_name: str,
    definition: str,
    search_vocabulary: list[str] | None = None,
    include_criteria: list[str],
    exclude_criteria: list[str],
    query_axes: list[str] | None = None,
    provider: str,
    query_count: int,
    model: str,
    query_limits: dict | None = None,
) -> GeneratedQueries:
    """generate_queries()의 비동기 버전 — 한 type의 tavily/serpapi 콜을 asyncio.gather로 동시에 보낼 때 쓴다."""
    inputs = _render_inputs(
        taxonomy_lv2=taxonomy_lv2, type_name=type_name, definition=definition,
        search_vocabulary=search_vocabulary, include_criteria=include_criteria,
        exclude_criteria=exclude_criteria, query_axes=query_axes, provider=provider, query_count=query_count,
    )
    output = await call_structured_output_async(client, prompt_cfg, model, **inputs)
    return _to_generated_queries(output, provider=provider, query_limits=query_limits)
