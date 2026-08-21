"""본문 정제 — raw_text에서 boilerplate만 걷어내고 정제 본문 3단을 채운다.

지도 원칙: 욕설·모욕·조롱·협박 표현은 taxonomy 신호이므로 **보존**한다.
제거 대상은 boilerplate(메뉴/푸터/관련글)뿐이며, PII 마스킹도 하지 않는다.
"""
from __future__ import annotations

import re

from src.common.schema import ContentRecord

# 반복 boilerplate (관련글/추천글/공유 등)
_BOILERPLATE = [
    re.compile(r"관련\s*글.*$", re.MULTILINE),
    re.compile(r"추천\s*글.*$", re.MULTILINE),
    re.compile(r"^(공유하기|스크랩|목록)\s*$", re.MULTILINE),
]

_NOISE_LINES = [
    re.compile(r"^(?:external/|/edit/).*$", re.I),
    re.compile(r".*(?:크리에이티브 커먼즈 라이선스|protected by reCAPTCHA|protected by hCaptcha|상세 내용 아이콘).*$", re.I),
    re.compile(r"^\s*\|?\s*(?:---+|\|\s*\|)\s*\|?\s*$"),
    re.compile(r"^[a-f0-9_-]{16,}$", re.I),
]

_MULTISPACE = re.compile(r"[ \t]{2,}")
_MULTINEWLINE = re.compile(r"\n{3,}")


def clean_record(rec: ContentRecord) -> ContentRecord:
    """raw_text(또는 parser의 core_text) 기준으로 cleaned_text/core_text/body_text를 채운다."""
    # Q&A parser가 만든 core_text가 있으면 배너·이미지·중복 답변이 섞인 raw_text보다 우선한다.
    cleaned = rec.core_text or rec.raw_text or rec.body_text
    for pat in _BOILERPLATE:
        cleaned = pat.sub("", cleaned)
    lines, seen = [], set()
    for line in cleaned.splitlines():
        normalized = line.strip()
        if any(pat.match(normalized) for pat in _NOISE_LINES) or normalized in seen:
            continue
        if normalized:
            seen.add(normalized)
        lines.append(line)
    cleaned = "\n".join(lines)
    cleaned = _MULTINEWLINE.sub("\n\n", _MULTISPACE.sub(" ", cleaned)).strip()

    rec.cleaned_text = cleaned
    rec.core_text = cleaned
    rec.body_text = cleaned

    rec.dedup_hash = rec.compute_dedup_hash()
    return rec
