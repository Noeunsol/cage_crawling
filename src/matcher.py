"""Phase 10 — Taxonomy Matching. 수집 의도 taxonomy와 실제 본문 적합성 검증.

1차는 rule-based mock. 나중에 LLM으로 교체할 수 있게 TaxonomyMatcher 인터페이스 유지.
confidence 임계값 → pass / review / fail 은 pipeline에서 적용 (설계서 §10).
"""
from __future__ import annotations

from .policy import Subtype
from .schema import ContentRecord, MatchResult

_SENSITIVE_HINTS = ("고소", "신고", "괴롭힘", "피해")


class TaxonomyMatcher:
    """교체 가능한 인터페이스. LLM matcher도 이 시그니처를 따른다."""
    def match(self, taxonomy_lv2: str, subtype: Subtype, rec: ContentRecord) -> MatchResult:
        raise NotImplementedError


class RuleBasedMatcher(TaxonomyMatcher):
    def __init__(self, review_threshold: float = 0.5):
        self.review_threshold = review_threshold

    def match(self, taxonomy_lv2, subtype, rec):
        text = f"{rec.title}\n{rec.body_text}"
        kw_hits = sum(1 for k in subtype.keywords if k in text)
        pos_hits = sum(1 for p in subtype.positive_patterns if p in text)
        neg_hits = sum(1 for n in subtype.negative_patterns if n in text)

        base = 0.4 if kw_hits else 0.2
        confidence = base + 0.15 * min(kw_hits, 3) + 0.1 * min(pos_hits, 3) - 0.3 * neg_hits
        confidence = round(max(0.0, min(confidence, 0.99)), 3)

        rec.taxonomy_relevance_score = confidence
        safety_flags = ["contains_sensitive_context"] if any(h in text for h in _SENSITIVE_HINTS) else []
        reason = f"keyword_hits={kw_hits}, positive={pos_hits}, negative={neg_hits}"

        return MatchResult(
            is_relevant=confidence >= self.review_threshold,
            taxonomy_lv2=taxonomy_lv2,
            subtype=subtype.name,
            confidence=confidence,
            reason=reason,
            safety_flags=safety_flags,
        )
