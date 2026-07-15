"""Phase 8 — Content Cleaning. boilerplate 제거 + 공백 정리 + PII 마스킹.

Risk Taxonomy 데이터는 민감정보가 섞일 수 있어 저장 전 마스킹은 필수.
"""
from __future__ import annotations

import re

from .schema import ContentRecord

# 반복 boilerplate 예시 (관련글/추천글/공유 등)
_BOILERPLATE = [
    re.compile(r"관련\s*글.*$", re.MULTILINE),
    re.compile(r"추천\s*글.*$", re.MULTILINE),
    re.compile(r"^(공유하기|스크랩|목록)\s*$", re.MULTILINE),
]

# PII 패턴. 순서 주의: 주민번호를 전화번호보다 먼저 (하이픈 형태 겹침 방지)
_PII = [
    (re.compile(r"\b\d{6}-\d{7}\b"), "[RRN]"),                       # 주민등록번호
    (re.compile(r"\b(?:\d{2,3}-)?\d{3,4}-\d{4}\b"), "[PHONE]"),       # 전화번호
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),         # 이메일
    (re.compile(r"\b\d{2,6}-\d{2,6}-\d{2,7}\b"), "[ACCOUNT]"),        # 계좌번호(포괄)
    (re.compile(r"[가-힣]+(?:특별시|광역시|도)\s?[가-힣]+(?:시|군|구)\s?[가-힣0-9]+(?:로|길)\s?\d+"), "[ADDRESS]"),
]

_MULTISPACE = re.compile(r"[ \t]{2,}")
_MULTINEWLINE = re.compile(r"\n{3,}")


def clean_record(rec: ContentRecord) -> ContentRecord:
    """rec.body_text를 정제하고 PII 마스킹. 제자리 수정 후 반환."""
    text = rec.body_text
    for pat in _BOILERPLATE:
        text = pat.sub("", text)
    text = mask_pii(text)
    text = _MULTISPACE.sub(" ", text)
    text = _MULTINEWLINE.sub("\n\n", text)
    rec.body_text = text.strip()
    # dedup 해시는 정제 후 본문 기준으로 재계산
    rec.dedup_hash = rec.compute_dedup_hash()
    return rec


def mask_pii(text: str) -> str:
    for pat, repl in _PII:
        text = pat.sub(repl, text)
    return text
