"""Phase 9 — Quality Filtering. 언어/한국관련성/길이/중복/품질점수로 저장 여부 판단.

fail이면 content_records 저장 안 함 (filter_logs만). 점수는 record에 채워 저장/리포트에 사용.
"""
from __future__ import annotations

import re

from .schema import ContentRecord, FilterResult

_HANGUL = re.compile(r"[가-힣]")
_NONSPACE = re.compile(r"\S")


class QualityFilter:
    def __init__(self, settings: dict):
        q = settings.get("quality", {})
        self.min_chars = q.get("min_body_chars", 300)
        self.min_korean_ratio = q.get("min_korean_ratio", 0.3)
        self.min_quality_score = q.get("min_quality_score", 0.6)
        self._seen_hashes: set[str] = set()

    def check(self, rec: ContentRecord) -> FilterResult:
        body = rec.body_text
        korean_ratio = _korean_ratio(body)
        rec.language = "ko" if korean_ratio >= self.min_korean_ratio else "other"
        rec.korea_relevance_score = round(korean_ratio, 3)
        rec.quality_score = self._score(rec, korean_ratio)

        # 중복
        if rec.dedup_hash in self._seen_hashes:
            return FilterResult("fail", "duplicate", rec.quality_score)
        # 길이
        if len(body) < self.min_chars:
            return FilterResult("fail", f"too_short:{len(body)}", rec.quality_score)
        # 언어
        if rec.language != "ko":
            return FilterResult("fail", f"not_korean:{korean_ratio:.2f}", rec.quality_score)
        # 품질 점수
        if rec.quality_score < self.min_quality_score:
            return FilterResult("fail", f"low_quality:{rec.quality_score:.2f}", rec.quality_score)

        self._seen_hashes.add(rec.dedup_hash)
        return FilterResult("pass", None, rec.quality_score)

    def _score(self, rec: ContentRecord, korean_ratio: float) -> float:
        """길이/한글비율/제목/날짜 유무 가중합. 0~1."""
        length_score = min(len(rec.body_text) / 800, 1.0)
        title_score = 1.0 if rec.title.strip() else 0.0
        date_score = 1.0 if rec.published_at else 0.5   # 날짜 없어도 절반 인정 (누락 허용)
        score = 0.4 * length_score + 0.3 * korean_ratio + 0.15 * title_score + 0.15 * date_score
        return round(score, 3)


def _korean_ratio(text: str) -> float:
    nonspace = _NONSPACE.findall(text)
    if not nonspace:
        return 0.0
    hangul = _HANGUL.findall(text)
    return len(hangul) / len(nonspace)
