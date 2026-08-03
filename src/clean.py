"""Phase 8 — Content Cleaning. raw → cleaned → masked 3단 분리.

지도 원칙: 욕설·모욕·조롱·협박 표현은 taxonomy 신호이므로 **보존**한다.
제거/마스킹 대상은 boilerplate(메뉴/푸터/관련글)와 PII(전화/이메일/주민번호/계좌/주소)뿐.
  raw_text     — 추출 원문 (불변)
  cleaned_text — boilerplate 제거 + 공백 정리
  masked_text  — cleaned + PII 마스킹 (matcher/LLM 입력, body_text 별칭)
마스킹 로직은 mask.py로 분리.
"""
from __future__ import annotations

import re

from .mask import BasicPIIMasker, Masker, risk_for_types
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


def _process_comments(comments: list[str], title: str, body: str, cfg: dict) -> tuple[list[str], int, int]:
    """완전 중복·무관 댓글을 선택적으로 제거하고 관련도순으로 안정 정렬한다."""
    title_tokens = set(_TOKEN.findall(title or ""))
    body_tokens = set(_TOKEN.findall(body or ""))

    def score(comment: str) -> tuple[float, bool]:
        tokens = set(_TOKEN.findall(comment or ""))
        overlap = len(tokens & body_tokens) + 2 * len(tokens & title_tokens)
        normalized = overlap / max(len(tokens), 1)
        protected = bool(_CONTEXT_COMMENT.search(comment or "") or _SENSITIVE_COMMENT.search(comment or ""))
        context = 0.75 if protected else 0.0
        # taxonomy를 정하지 않고 위험/반박 맥락 댓글을 앞쪽에 보존한다.
        sensitive = 0.5 if _SENSITIVE_COMMENT.search(comment or "") else 0.0
        noise = 1.0 if _SHORT_REACTION.fullmatch((comment or "").strip()) else 0.0
        advertising = 1.0 if _COMMENT_AD.search(comment or "") else 0.0
        return normalized + context + sensitive - noise - advertising, protected

    deduped, seen, duplicate_removed = [], set(), 0
    for comment in comments:
        key = _MULTISPACE.sub(" ", (comment or "").strip().lower())
        if cfg.get("drop_duplicates", True) and key in seen:
            duplicate_removed += 1
            continue
        seen.add(key)
        deduped.append(comment)

    min_score = float(cfg.get("min_score", 0.08))
    kept, unrelated_removed = [], 0
    for index, comment in enumerate(deduped):
        relevance, protected = score(comment)
        if cfg.get("drop_unrelated", True) and relevance < min_score and not protected:
            unrelated_removed += 1
            continue
        kept.append((index, relevance, comment))
    kept.sort(key=lambda row: (-row[1], row[0]))
    return [row[2] for row in kept], duplicate_removed, unrelated_removed


def clean_record(rec: ContentRecord, policy: dict | None = None,
                 masker: Masker | None = None, comment_config: dict | None = None) -> ContentRecord:
    """raw_text 기준으로 cleaned_text/masked_text/masked_comments 채운다. 제자리 수정.

    policy: subtype.preservation_policy (mask_pii/mask_credentials 등). None이면 기본(PII+credential 마스킹).
    """
    cleaned = rec.raw_text or rec.body_text
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

    masker = masker or BasicPIIMasker()
    # Global privacy 설정이 안전 하한선이다. taxonomy 정책은 이를 해제할 수 없다.
    overrides = {}

    result = masker.mask(cleaned, overrides) if isinstance(masker, BasicPIIMasker) else masker.mask(cleaned)
    rec.cleaned_text = cleaned
    rec.masked_text = result.masked_text
    rec.body_text = rec.masked_text           # 별칭 (report/CSV/test 호환)
    entities = list(result.entities)
    pii_types = list(result.pii_types)
    warnings = list(result.warnings)
    if rec.raw_comments:
        rec.original_comment_count = len(rec.raw_comments)
        cfg = (comment_config or {}).get("relevance_filter", comment_config or {})
        if cfg.get("enabled", True):
            rec.raw_comments, rec.duplicate_comments_removed, rec.unrelated_comments_removed = (
                _process_comments(rec.raw_comments, rec.title, cleaned, cfg)
            )
        rec.kept_comment_count = len(rec.raw_comments)
        comment_results = [
            masker.mask(x, overrides) if isinstance(masker, BasicPIIMasker) else masker.mask(x)
            for x in rec.raw_comments
        ]
        rec.masked_comments = [x.masked_text for x in comment_results]
        for item in comment_results:
            entities.extend(item.entities)
            pii_types.extend(item.pii_types)
            warnings.extend(item.warnings)

    rec.masked_entities = entities
    rec.pii_detected = bool(entities)
    rec.pii_types = list(dict.fromkeys(pii_types))
    rec.pii_risk_score = risk_for_types(rec.pii_types)
    rec.masking_version = result.masking_version
    rec.masking_warnings = warnings

    rec.dedup_hash = rec.compute_dedup_hash()
    return rec
