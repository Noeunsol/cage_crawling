"""본문 점수 → 최종 action 판정 (3개 모드의 판정 규칙을 한 곳에).

세 함수는 각기 다른 정책 계약을 가진다 — 통합하지 말 것:
- _classification_status     : keyword(레거시) 모드. subtype threshold + pii 게이트 → pass|fail
- _trend_classification_action: trend 모드. korea 게이트 + 로컬 임계값 → accepted|discard
- _phase2_adjudicate         : gap_filling(2차) 모드. korea 독립 게이트 + opportunistic mismatch
"""
from __future__ import annotations

def _classification_status(match, rec, thresholds: dict, max_pii_risk: float) -> str:
    fit = rec.taxonomy_fit_score or 0
    harm = rec.harmfulness_score or 0
    val = rec.seed_source_value_score or 0
    pii = rec.pii_risk_score or 0
    t = thresholds or {}
    if (match.is_relevant
            and fit >= t.get("min_taxonomy_fit_score", 0.75)
            and harm >= t.get("min_harmfulness_score", 0.65)
            and val >= t.get("min_seed_source_value_score", 0.60)
            and pii <= max_pii_risk):
        return "pass"
    return "fail"


def _trend_classification_action(match, rec, settings: dict) -> str:
    """LLM 점수를 로컬 기준으로 재판정한다: accepted | discard."""
    if not match.is_relevant:
        return "discard"
    m = settings.get("matching", {})
    confidence = match.confidence
    fit = rec.taxonomy_fit_score or 0
    concrete = rec.concrete_context_score or 0
    korea = rec.korea_relevance_score or 0
    if korea < float(m.get("min_korea_relevance", 0.3)):
        return "discard"
    if rec.filter_status not in {"pass", "review"}:
        return "discard"
    if (confidence >= float(m.get("accepted_confidence", 0.75))
            and fit >= float(m.get("accepted_taxonomy_fit", 0.70))
            and concrete >= float(m.get("accepted_concrete_context", 0.50))):
        return "accepted"
    return "discard"


def _phase2_adjudicate(match, rec, target_lv2: str, p2: dict, deficit_of) -> tuple[str, str]:
    """본문 기준 최종 판정. korea_relevance 독립 게이트 + opportunistic mismatch."""
    acc = p2.get("acceptance", {})
    predicted = match.taxonomy_lv2
    korea = rec.korea_relevance_score or 0
    fit = rec.taxonomy_fit_score or 0
    concrete = rec.concrete_context_score or 0
    if not match.is_relevant:
        return "discard", "not_relevant"
    if korea < float(acc.get("min_korea_relevance_score", 0.6)):
        return "discard", "low_korea_relevance"
    if (fit >= float(acc.get("min_taxonomy_fit_score", 0.75))
            and concrete >= float(acc.get("min_concrete_context_score", 0.6))):
        action = "accepted"
    else:
        return "discard", "low_taxonomy_fit"
    if predicted != target_lv2:   # opportunistic: 자체 accepted 등급 + predicted 부족일 때만
        allow = p2.get("adjudication", {}).get("allow_opportunistic_accept", True)
        if action == "accepted" and allow and deficit_of(predicted) > 0:
            return "accepted", f"opportunistic:{target_lv2}->{predicted}"
        return "discard", f"mismatch_not_qualified:{target_lv2}->{predicted}"
    return action, "matched"
