"""근사 중복(재게시·경미 수정) 탐지용 제목 정규화 + 유사도 (10.3절 확장, 2026-08-31).

형태소 분석기 의존성을 피하려고 제목은 문자 n-gram cosine을, 본문은 단어 단위 Jaccard를 쓴다.

# ponytail: 처음엔 본문에 64bit SimHash(해밍 거리)를 썼는데, 실제 한국어 기사(150~200자)로
# 재보니 재게시(0.65~0.70)와 완전 별개 기사(0.45~0.61)가 거의 안 갈렸다 — 짧은 텍스트에서는
# bit-voting 신호가 너무 약함. 단어 unigram Jaccard로 바꾸니 같은 비교에서 0.42 vs 0.02~0.03으로
# 15~20배 벌어져 훨씬 잘 갈린다(절대값은 낮아도 상대 구분력이 핵심). 지금 규모(수천 건)면
# 지문(단어 집합)끼리 직접 Jaccard를 계산해도 충분히 빠르다 — 업그레이드 트리거(2026-08-31 합의):
# 코퍼스가 2만 건을 넘거나, 후보 1건당 근사중복 비교(check_near_duplicate 1회 호출)가 200ms를
# 넘으면 그때 MinHash(고정 크기 서명, O(1) 비교)로 바꾼다. 둘 다 아직이면 이대로 둔다.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_BRACKET_TAG = re.compile(r"\[[^\]]{0,12}\]")               # [단독], [속보], [영상] 등
_TRAILING_OUTLET = re.compile(r"\s*[-|·]\s*[^-|·]{1,20}$")   # 끝의 " - 언론사명" / " | 언론사명"
_REPOST_MARKER = re.compile(r"(모바일|재게시|전재|송고)\s*[:\-]?\s*")
_REPEAT_PUNCT = re.compile(r"([!?.…,~])\1{1,}")
_WHITESPACE = re.compile(r"\s+")
_WORD = re.compile(r"\S+")


def normalize_title(title: str) -> str:
    """[단독]/[속보] 같은 태그, 끝에 붙는 언론사명, 반복 특수문자, 재게시 표기를 제거한다.

    언론사명/기자명은 고정 목록이 없어 완벽히 걸러내진 못한다 — 흔한 " - 언론사" / " | 언론사"
    꼬리표 패턴만 처리한다 (완벽보다 실용, 나머지는 n-gram cosine의 관용도로 흡수된다).
    """
    t = _BRACKET_TAG.sub("", title)
    t = _TRAILING_OUTLET.sub("", t)
    t = _REPOST_MARKER.sub("", t)
    t = _REPEAT_PUNCT.sub(r"\1", t)
    t = _WHITESPACE.sub(" ", t).strip()
    return t


def _char_ngrams(text: str, n: int = 2) -> Counter:
    chars = [c for c in text if not c.isspace()]
    if len(chars) < n:
        return Counter(chars)
    return Counter("".join(chars[i:i + n]) for i in range(len(chars) - n + 1))


def title_similarity(normalized_title_a: str, normalized_title_b: str) -> float:
    """정규화된 제목 두 개의 문자 2-gram cosine 유사도 (0~1)."""
    a, b = _char_ngrams(normalized_title_a), _char_ngrams(normalized_title_b)
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b[k] for k in a.keys() & b.keys())
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def content_fingerprint(content: str) -> frozenset[str]:
    """본문의 고유 단어(공백 기준 토큰) 집합 — DB에는 이걸 직렬화해서 저장한다."""
    return frozenset(_WORD.findall(content))


def serialize_fingerprint(fingerprint: frozenset[str]) -> str:
    return " ".join(sorted(fingerprint))


def deserialize_fingerprint(text: str | None) -> frozenset[str]:
    return frozenset(text.split()) if text else frozenset()


def content_similarity(fingerprint_a: frozenset[str], fingerprint_b: frozenset[str]) -> float:
    """두 단어 집합의 Jaccard 유사도 (0~1)."""
    if not fingerprint_a or not fingerprint_b:
        return 0.0
    return len(fingerprint_a & fingerprint_b) / len(fingerprint_a | fingerprint_b)
