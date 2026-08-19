"""Phase 8 — Content Cleaning.

지도 원칙: 욕설·모욕·조롱·협박 표현은 taxonomy 신호이므로 **보존**한다.
제거 대상은 boilerplate(메뉴/푸터/관련글)이며, 정제 본문을 분류·저장에 사용한다.
"""
from __future__ import annotations

import re

from .schema import ContentRecord

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
_TOKEN = re.compile(r"[가-힣A-Za-z0-9]{2,}")
_CONTEXT_COMMENT = re.compile(r"사실\s*아니|오보|원본|정정|반박|해명|피해|신고|증거|목격")
_SHORT_REACTION = re.compile(r"^(?:ㅋ+|ㅎ+|ㅠ+|ㅜ+|ㅇㅇ|ㄹㅇ|인정|첫댓|ㄷㄷ|[!?~.]+)$")
_SENSITIVE_COMMENT = re.compile(r"협박|스토킹|신상|유출|폭행|성범죄|자살|자해|사기|해킹")
_COMMENT_AD = re.compile(r"https?://|오픈채팅|카톡\s*문의|텔레그램|광고\s*문의|수익\s*보장", re.I)




def clean_record(rec: ContentRecord, policy: dict | None = None) -> ContentRecord:
    """raw_text 기준으로 정제 본문을 채운다. policy는 호출 호환용이며 사용하지 않는다."""
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
    rec.masked_text = cleaned  # 기존 DB/LLM 입력 필드 호환용 별칭. 마스킹은 수행하지 않는다.
    rec.body_text = cleaned

    rec.dedup_hash = rec.compute_dedup_hash()
    return rec
