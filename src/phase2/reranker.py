"""Search result reranker (fetch 전 필터). rule prefilter 먼저 → 애매한 후보만 LLM.

목적: semantic provider가 가져온 후보 중 무관/비한국 콘텐츠를 fetch/extract 비용 전에 걸러낸다.
rule로 명백한 고/저관련을 확정하고, 임계 근처의 애매한 후보에만 LLM을 1회 호출한다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

from ..prompt_loader import PromptSpec

log = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def _rerank_spec(path: str = "prompts/discovery_rerank.yaml"):
    """rerank LLM 프롬프트 정의(yaml). path별 1회 로드."""
    return PromptSpec.load(path)

# 한국 플랫폼 도메인(정확 판정용). 그 외는 한글 비율로 근사.
_KOREA_DOMAINS = {
    "naver.com", "daum.net", "dcinside.com", "pann.nate.com", "nate.com", "fmkorea.com",
    "instiz.net", "ruliweb.com", "inven.co.kr", "lawtalk.co.kr", "tistory.com", "clien.net",
    "yna.co.kr", "newsis.com", "kisa.or.kr", "namu.wiki", "humoruniv.com",
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "discovery_relevance_score": {"type": "number"},
        "korea_relevance_score": {"type": "number"},
        "fetch_decision": {"type": "string", "enum": ["fetch", "low_priority", "skip"]},
        "reason": {"type": "string"},
    },
    "required": ["discovery_relevance_score", "korea_relevance_score", "fetch_decision", "reason"],
    "additionalProperties": False,
}


@dataclass
class RerankResult:
    discovery_relevance_score: float
    korea_relevance_score: float
    fetch_decision: str          # fetch | low_priority | skip
    source: str                  # rule | llm
    reason: str


def _hangul_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum("가" <= c <= "힣" for c in letters) / len(letters)


def _korea_domain(url: str) -> bool:
    host = urlparse(url or "").netloc.lower().removeprefix("www.")
    return any(host == d or host.endswith(f".{d}") for d in _KOREA_DOMAINS)


def _decide(disc: float, korea: float, cfg: dict) -> str:
    # 0.40 미만은 Tavily 검색 품질 확인용으로도 너무 넓어 본문을 가져오지 않는다.
    # 그 이상이면서 우선점수 미만인 후보만 fetch 여유가 있을 때 low_priority로 처리한다.
    if korea < cfg["min_korea_relevance"]:
        return "skip"
    if disc < cfg["min_discovery_relevance_floor"]:
        return "skip"
    if disc >= cfg["min_discovery_relevance"] and korea >= cfg["min_korea_relevance"]:
        return "fetch"
    return "low_priority"


def _include_terms(intent, result) -> list[str]:
    """이 후보를 데려온 쿼리의 type에 맞는 가점 어휘. type별이 없으면 LV2 공통."""
    subtype = intent.query_types.get(result.query_or_intent, "")
    return intent.include_by_type.get(subtype) or intent.include


def _rule_scores(result, intent) -> tuple[float, float, int, int]:
    text = " ".join(filter(None, [result.title, result.snippet, result.content_hint])).lower()
    inc = sum(1 for t in _include_terms(intent, result) if t and t.lower() in text)
    exc = sum(1 for t in intent.exclude if t and t.lower() in text)
    base = float(result.provider_score) if result.provider_score is not None else 0.5
    disc = max(0.0, min(1.0, base + 0.15 * min(inc, 3) - 0.3 * min(exc, 2)))
    hr = _hangul_ratio(text)
    korea = 0.9 if _korea_domain(result.url) or hr >= 0.5 else (0.6 if hr >= 0.2 else 0.2)
    return round(disc, 3), round(korea, 3), inc, exc


def rerank(result, intent, llm=None, cfg: dict | None = None) -> RerankResult:
    cfg = {
        "min_discovery_relevance": 0.65, "min_discovery_relevance_floor": 0.40,
        "min_korea_relevance": 0.60,
        "llm_margin": 0.15, **(cfg or {}),
    }
    disc, korea, inc, exc = _rule_scores(result, intent)

    # ① rule로 확정 가능한 명백한 경우
    if exc >= 2 and inc == 0:
        return RerankResult(disc, korea, "skip", "rule", f"exclude_hits={exc}, no include")
    margin = cfg["llm_margin"]
    near = (abs(disc - cfg["min_discovery_relevance"]) < margin
            or abs(korea - cfg["min_korea_relevance"]) < margin)
    if not near or llm is None:
        return RerankResult(disc, korea, _decide(disc, korea, cfg), "rule",
                            f"include={inc}, exclude={exc}")

    # ② 애매 band만 LLM 호출
    spec = _rerank_spec()
    system = spec.render_system(
        target_lv2=intent.target_taxonomy_lv2,
        goal=" | ".join(intent.queries),
        include=", ".join(_include_terms(intent, result)[:12]),
        exclude=", ".join(intent.exclude[:12]),
    )
    user = spec.render_user(
        title=result.title, snippet=result.snippet or "",
        hint=result.content_hint or "", url=result.url,
    )
    data = llm._complete_json(system, user, _SCHEMA) if hasattr(llm, "_complete_json") else None
    if not data:
        return RerankResult(disc, korea, _decide(disc, korea, cfg), "rule", "llm_unavailable->rule")
    ld = round(max(0.0, min(float(data.get("discovery_relevance_score", disc)), 1.0)), 3)
    lk = round(max(0.0, min(float(data.get("korea_relevance_score", korea)), 1.0)), 3)
    decision = data.get("fetch_decision") or _decide(ld, lk, cfg)
    return RerankResult(ld, lk, decision, "llm", data.get("reason", ""))


if __name__ == "__main__":
    from dataclasses import dataclass as _dc

    from .intent_builder import CollectionIntent

    @_dc
    class _R:
        title: str; snippet: str; content_hint: str; url: str; provider_score: float
        query_or_intent: str = "개인정보 유출 피해"

    intent = CollectionIntent("4_I_Privacy_Infringement", queries=["개인정보 유출 피해"],
                              include=["신상털이", "개인정보 유출"], exclude=["광고", "처리방침"])
    # 명백 고관련 한국 도메인 → rule로 fetch (LLM 없이)
    hi = rerank(_R("신상털이 피해 고소", "개인정보 유출로 피해", "신상털이 개인정보 유출", "https://pann.nate.com/1", 0.9), intent)
    assert hi.fetch_decision == "fetch" and hi.source == "rule", hi
    # 명백 제외 → skip
    lo = rerank(_R("보안 솔루션 광고", "기업 개인정보 처리방침 광고 광고", "광고 처리방침", "https://ads.example.com/1", 0.4), intent)
    assert lo.fetch_decision == "skip", lo
    print("reranker self-check OK")
