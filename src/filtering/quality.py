"""Phase 9 — Quality Filtering. 언어/한국관련성/길이/중복/품질점수로 저장 여부 판단.

fail이면 content_records 저장 안 함 (filter_logs만). 점수는 record에 채워 저장/리포트에 사용.
"""
from __future__ import annotations

import re

from ..schema import ContentRecord, FilterResult

_HANGUL = re.compile(r"[가-힣]")
_NONSPACE = re.compile(r"\S")


class QualityFilter:
    def __init__(self, settings: dict):
        q = settings.get("quality", {})
        self.min_chars = q.get("min_body_chars", 300)
        # 커뮤니티 글은 원래 짧다. 전역 하한만 두면 extraction은 통과시킨 글을 여기서 버려
        # fetch·추출 비용만 쓰고 끝난다. extraction.success_criteria와 짝을 맞춘다.
        self.min_chars_by_site_type = q.get("min_body_chars_by_site_type", {}) or {}
        self.min_korean_ratio = q.get("min_korean_ratio", 0.3)
        self.min_quality_score = q.get("min_quality_score", 0.6)
        self._seen_hashes: set[tuple[str, str]] = set()

    def _min_chars_for(self, site_type: str) -> int:
        return int(self.min_chars_by_site_type.get(site_type, self.min_chars))

    def check(self, rec: ContentRecord) -> FilterResult:
        body = _effective_text(rec)
        min_chars = self._min_chars_for(rec.site_type)
        korean_ratio = _korean_ratio(body)
        rec.language = "ko" if korean_ratio >= self.min_korean_ratio else "other"
        rec.korea_relevance_score = round(korean_ratio, 3)
        rec.quality_score = self._score(rec, korean_ratio, body)

        # 중복
        dedup_key = (rec.subtype_candidate, rec.dedup_hash)
        if dedup_key in self._seen_hashes:
            return FilterResult("fail", "duplicate", rec.quality_score)
        # 길이
        if len(body) < min_chars:
            return FilterResult("fail", f"too_short:{len(body)}", rec.quality_score)
        # 언어
        if rec.language != "ko":
            return FilterResult("fail", f"not_korean:{korean_ratio:.2f}", rec.quality_score)
        # 품질 점수
        if rec.quality_score < self.min_quality_score:
            return FilterResult("fail", f"low_quality:{rec.quality_score:.2f}", rec.quality_score)

        self._seen_hashes.add(dedup_key)
        return FilterResult("pass", None, rec.quality_score)

    def _score(self, rec: ContentRecord, korean_ratio: float, body: str) -> float:
        """길이/한글비율/제목/날짜 유무 가중합. 0~1."""
        length_score = min(len(body) / 800, 1.0)
        title_score = 1.0 if rec.title.strip() else 0.0
        date_score = 1.0 if rec.published_at else 0.5   # 날짜 없어도 절반 인정 (누락 허용)
        score = 0.4 * length_score + 0.3 * korean_ratio + 0.15 * title_score + 0.15 * date_score
        return round(score, 3)


# 자모/웃음/감탄만으로 이뤄진 잡담 판별용
_CHATTER = re.compile(r"[ㅋㅎㅠㅜㅇㄹㄴㅅㅂㄷㅗㅜ\s\.\,\!\?~ㅡ0-9]+")
_URL = re.compile(r"https?://\S+")


def basic_filter(rec: ContentRecord, min_len: int = 20) -> FilterResult:
    """트렌드 모드 1차 basic filter: 너무 짧음/링크만/이미지만/단순잡담 제거.

    이미지 전용 글은 추출 후 본문이 비어 too_short로 걸린다.
    """
    body = rec.masked_text or rec.body_text or ""
    combined = f"{rec.title} {body}".strip()
    text_wo_url = _URL.sub("", combined).strip()
    if _URL.search(combined) and len(text_wo_url) < 10:
        return FilterResult("fail", "link_only")
    if len(combined) < min_len:
        return FilterResult("fail", f"too_short:{len(combined)}")
    if len(_CHATTER.sub("", combined)) < 5:
        return FilterResult("fail", "chatter")
    return FilterResult("pass", None)


def _effective_text(rec: ContentRecord) -> str:
    return rec.masked_text or rec.body_text or ""


def _korean_ratio(text: str) -> float:
    nonspace = _NONSPACE.findall(text)
    if not nonspace:
        return 0.0
    hangul = _HANGUL.findall(text)
    return len(hangul) / len(nonspace)
