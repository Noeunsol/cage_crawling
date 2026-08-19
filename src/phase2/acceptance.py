"""2차 targeted 저장 게이트 — OpenAI 본문 분류 대신 쓰는 결정론적 검증.

저장 조건은 네 가지다.
  1) domestic_direct  : 한국에서 벌어졌거나 한국의 피해·대응이 있는 사건인가
  2) 목표 LV2 evidence: 목표 LV2의 행위 신호가 본문에 실제로 있는가
  3) recency          : LV2별 recency_days 안의 콘텐츠인가 (날짜 미상은 통과)
  4) 구체 맥락        : 정의·안내문이 아니라 구체 사건인가

통과한 레코드의 taxonomy_lv2는 "모델이 예측한 값"이 아니라
"검색 목표 LV2가 이 게이트를 통과해 확정된 값"이다. gap_filling이 그 provenance를
classification_source="targeted_acceptance_gate"로 기록한다.

Type은 최종 라벨이 아니므로 목표 Type과 다른 Type 신호가 잡혀도 같은 LV2면 통과시킨다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..filtering.korea_context import score_korea_context
from .reranker import _term_hits

# 정의·안내문이 아니라 실제 사건임을 보이는 최소 어휘. 하나라도 없으면 로컬라이제이션 가치가 낮다.
_CONCRETE_TERMS = (
    "피해", "사건", "사고", "신고", "고소", "고발", "수사", "조사", "검거", "기소",
    "판결", "처벌", "논란", "적발", "발생", "당했", "겪었", "상담", "문의", "질문",
)
# 공식 한국 기관 도메인은 소스만으로 domestic_direct 근거가 된다.
_OFFICIAL_SUFFIXES = (".go.kr", ".or.kr")


@dataclass
class AcceptanceResult:
    accepted: bool
    reason: str
    korea_evidence: list[str] = field(default_factory=list)
    lv2_evidence: list[str] = field(default_factory=list)


def _reject(reason: str) -> AcceptanceResult:
    return AcceptanceResult(False, reason)


def _body_of(rec) -> str:
    return rec.core_text or rec.masked_text or rec.body_text or ""


def _official_source(url: str) -> bool:
    host = (url or "").split("//")[-1].split("/")[0].lower()
    return any(host.endswith(suffix) for suffix in _OFFICIAL_SUFFIXES)


def _lv2_terms(candidate, strategy: dict) -> list[str]:
    """계획된 expected_lv2_evidence + 전략의 type별 가점 어휘."""
    terms = list(getattr(candidate, "expected_lv2_evidence", []) or [])
    by_type = strategy.get("include_by_type") or {}
    target_type = getattr(candidate, "target_type", "")
    # 목표 Type 어휘를 앞에 두되, Type은 최종 라벨이 아니므로 같은 LV2의 다른 Type 신호도 인정한다.
    terms += by_type.get(target_type) or []
    for name, values in by_type.items():
        if name != target_type:
            terms += values
    terms += strategy.get("include") or []
    return list(dict.fromkeys(terms))


def _age_days(published_at: str | None, today: date) -> int | None:
    """YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD 및 뒤에 시각이 붙은 형태를 모두 받는다."""
    if not published_at:
        return None
    text = published_at.strip().replace(".", "-").replace("/", "-")[:10]
    try:
        return (today - date.fromisoformat(text)).days
    except ValueError:
        return None


def evaluate(rec, candidate, strategy: dict, policy: dict,
             today: date | None = None) -> AcceptanceResult:
    """strategy = source_strategies_by_lv2[lv2], policy = default_acceptance."""
    today = today or date.today()
    body = _body_of(rec)
    accept = {**policy, **(strategy.get("acceptance") or {})}

    # [1] domestic_direct
    korea_score, korea_evidence = score_korea_context(rec.title, body, rec.source_url)
    if _official_source(rec.source_url):
        korea_evidence = ["한국 공식기관 출처", *korea_evidence]
    korea_evidence += [
        f"계획 근거:{term}"
        for term in _term_hits(f"{rec.title} {body}", getattr(candidate, "expected_korea_evidence", []) or [])
    ]
    domestic = _official_source(rec.source_url) or korea_score >= float(accept.get("min_korea_score", 0.6))
    if accept.get("require_domestic_direct", True) and not domestic:
        return _reject(f"not_domestic_direct:{korea_score}")

    # [2] 목표 LV2 evidence
    lv2_evidence = _term_hits(f"{rec.title} {body}", _lv2_terms(candidate, strategy))
    if accept.get("require_lv2_evidence", True) and not lv2_evidence:
        return _reject("missing_lv2_evidence")

    # [3] recency — 날짜를 못 읽은 커뮤니티 글까지 버리지는 않는다.
    recency_days = strategy.get("recency_days")
    age = _age_days(rec.published_at, today)
    if recency_days and age is not None and age > int(recency_days):
        return _reject(f"stale:{age}d")

    # [4] 구체 맥락
    if len(body) < int(accept.get("min_body_chars", 200)):
        return _reject(f"low_localization_value:short:{len(body)}")
    if not _term_hits(f"{rec.title} {body}", _CONCRETE_TERMS):
        return _reject("low_localization_value:no_incident_context")

    return AcceptanceResult(True, "targeted_acceptance_gate", korea_evidence, lv2_evidence)
