"""Fast, rule-based relevance gate. It never assigns taxonomy labels.

출력은 keep / discard 2-way다. keep은 LLM으로 넘기고, discard는 원문을 버린다.
is_taxonomy_relevant는 "명확한 위험신호로 keep"인지(True), "애매해서 keep"(pii/이미지/댓글의존, False)인지 구분한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from src.common.filtering.risk_signals import RISK_SIGNAL_KEYWORDS, SIGNAL_TO_LV2


# 위험신호 어휘는 risk_signals.py가 단일 정본이다(1차 gate·2차 acceptance 공용).
# 옛 이름 RELEVANCE_SIGNAL_TO_LV2는 호출부 호환을 위한 별칭이다.
RELEVANCE_SIGNAL_TO_LV2 = SIGNAL_TO_LV2

NEGATIVE_CONTEXT_KEYWORDS = {
    "prevention_policy": [
        "예방", "대책", "점검", "보완", "정책", "정부", "관계부처", "상담", "지원",
        "캠페인", "교육", "신고센터", "피해자 지원", "단속", "처벌 강화", "보호", "안전관리",
    ],
    "news_neutral_report": [
        "기자", "보도", "발표", "회의", "국무회의", "브리핑", "관계자는", "당부했다",
        "밝혔다", "설명했다",
    ],
}

_DIRECT_RISK_TERMS = [
    "죽고 싶", "죽고싶", "자살 방법", "자살방법", "동반자살", "목숨 끊", "유서",
    "죽여", "살해하", "털자", "공개하자", "공유하자",
]
_TREND_TERMS = [
    "논란", "갈등", "범죄", "사건", "사고", "폭염", "정책", "젠더", "기업", "연예",
    "정치", "경제", "ai", "보안", "해킹", "개인정보", "불매", "사과", "의혹",
]
_CONTROVERSY_TERMS = ["논란", "근황", "사과문", "실베", "난리", "폭로", "저격", "반응", "불매", "해명", "사건"]
_AD_TERMS = ["광고 문의", "협찬", "프로모션", "쿠팡 파트너스", "구매 링크", "특가 판매"]
_URL_ONLY = re.compile(r"^(?:\s*https?://\S+\s*)+$", re.I)
_MEDIA_EXTENSIONS = (".mp4", ".webm", ".mov", ".avi", ".mkv", ".m3u8")
_VIDEO_HOSTS = {
    "youtube.com", "youtu.be", "youtube-nocookie.com", "tv.naver.com",
    "vimeo.com", "twitch.tv",
}

# 제목 1차 gate는 taxonomy를 배정하지 않고 19종 중 하나와 관련될 가능성만 넓게 포착한다.
_TITLE_EXTRA_TERMS = [
    # child/discrimination/advisory
    "아동학대", "아동 성착취", "미성년자 성착취", "그루밍", "차별", "채용 배제",
    "의료 조언", "법률 조언", "투자 추천", "종목 추천",
    # sensitive/IP/unethical
    "기밀", "내부문서", "내부 자료", "영업비밀", "소스코드 유출", "저작권 침해",
    "개인정보 유출", "개인정보", "기술 유출", "도면 유출", "내부자료 유출", "영업비밀 유출",
    "불법 다운로드", "위조상품", "표절", "시험 부정", "대리시험", "조작적 설득",
    # CBRNE / system integrity
    "폭발물 제조", "폭탄 제조", "생물무기", "화학무기", "핵무기", "방사능 테러",
    "독극물", "살충제", "치사량", "약물 혼합", "음식물 투입", "약물 투여", "커피에", "치사량 검색",
    "범행 수법", "흉기 사용", "상해 유발",
    "프롬프트 인젝션", "프롬프트 공격", "탈옥 프롬프트", "시스템 프롬프트",
    "시스템 지침 유출", "무한 루프", "api 폭주", "자원 고갈",
]


def is_textless_media_url(url: str) -> bool:
    """텍스트 추출 대상이 아닌 동영상 URL인지 저비용으로 판정한다."""
    parsed = urlparse(url or "")
    host = parsed.netloc.lower().removeprefix("www.")
    return (
        any(host == domain or host.endswith(f".{domain}") for domain in _VIDEO_HOSTS)
        or parsed.path.lower().endswith(_MEDIA_EXTENSIONS)
    )


@dataclass
class RelevanceResult:
    is_taxonomy_relevant: bool
    is_trend_seed: bool
    filter_action: str            # keep | discard
    filter_reason: str
    risk_signals: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    negative_contexts: list[str] = field(default_factory=list)
    needs_comment_fallback: bool = False


def build_fast_filter_text(record) -> str:
    body = getattr(record, "core_text", "") or getattr(record, "body_text", "") or ""
    limit = 800 if getattr(record, "source_type", "") == "news" else 1000
    return "\n".join(filter(None, [
        getattr(record, "title", "") or "",
        getattr(record, "summary", "") or "",
        body[:limit],
        f"source={getattr(record, 'source', '')} source_type={getattr(record, 'source_type', '')} "
        f"board={getattr(record, 'board_name', '')} category={getattr(record, 'category_name', '')}",
    ])).lower()


def detect_risk_signals(text: str) -> tuple[list[str], list[str]]:
    found = {
        signal: [keyword for keyword in keywords if keyword.lower() in text]
        for signal, keywords in RISK_SIGNAL_KEYWORDS.items()
    }
    found = {signal: keywords for signal, keywords in found.items() if keywords}
    return list(found), list(dict.fromkeys(k for keywords in found.values() for k in keywords))


def detect_negative_context(text: str) -> list[str]:
    return [
        context for context, keywords in NEGATIVE_CONTEXT_KEYWORDS.items()
        if any(keyword.lower() in text for keyword in keywords)
    ]


def decide_filter_action(record) -> RelevanceResult:
    text = build_fast_filter_text(record)
    body = (getattr(record, "core_text", "") or getattr(record, "body_text", "") or "").strip()
    title = (getattr(record, "title", "") or "").strip()
    source_type = getattr(record, "source_type", "")

    if _URL_ONLY.fullmatch(body):
        return _result("discard", "link_only")
    if len(f"{title}{body}".strip()) < 20:
        return _result("discard", "too_short")
    if any(term in text for term in _AD_TERMS):
        return _result("discard", "advertisement")

    signals, matched = detect_risk_signals(text)
    negatives = detect_negative_context(text)
    if not signals:
        if source_type == "news" and any(term in text for term in _TREND_TERMS):
            return _result("discard", "news_trend_seed", seed=True)
        return _result("discard", "no_risk_signal")

    prevention_hits = sum(
        keyword.lower() in text for keyword in NEGATIVE_CONTEXT_KEYWORDS["prevention_policy"]
    )
    # One generic word such as "정부" must not suppress a real risk signal.
    safe_context = prevention_hits >= 2 and not any(term in text for term in _DIRECT_RISK_TERMS)
    if safe_context:
        return _result(
            "discard", "risk_keyword_but_policy_or_prevention_context",
            signals, matched, negatives, seed=source_type == "news",
        )

    weak_signals = set(signals) <= {"harassment", "rumor_or_misinformation"}
    fallback = weak_signals and (
        source_type == "community"
        and (getattr(record, "comment_count", 0) or 0) >= 50
        and any(term in text for term in _CONTROVERSY_TERMS)
        and len(body) < 100
    )
    if fallback:
        return _result("keep", "needs_comment_fallback", signals, matched, negatives, fallback=True)
    return _result("keep", "risk_signal_detected", signals, matched, negatives, relevant=True)


def decide_candidate_action(candidate) -> RelevanceResult:
    """본문 요청 전 제목만으로 19종 taxonomy 관련 가능성을 keep/discard한다."""
    if is_textless_media_url(getattr(candidate, "source_url", "")):
        return _result("discard", "video_without_text")
    title = (getattr(candidate, "title", "") or "").strip()
    if not title:
        return _result("discard", "missing_title")
    text = title.lower()
    signals, matched = detect_risk_signals(text)
    extras = [term for term in _TITLE_EXTRA_TERMS if term in text]
    if not signals and not extras:
        meta = getattr(candidate, "meta", {}) or {}
        source_type = meta.get("source_type") or getattr(candidate, "site_type", "")
        comment_count = int(meta.get("comment_count") or 0)
        if source_type == "community" and comment_count >= 50:
            return _result(
                "keep", "high_comment_community_needs_body",
                fallback=True, seed=True,
            )
        return _result("discard", "title_no_taxonomy_signal")
    negatives = detect_negative_context(text)
    prevention_hits = sum(
        keyword.lower() in text for keyword in NEGATIVE_CONTEXT_KEYWORDS["prevention_policy"]
    )
    if prevention_hits >= 2 and not any(term in text for term in _DIRECT_RISK_TERMS):
        return _result(
            "discard", "title_policy_or_prevention_context", signals,
            matched + extras, negatives,
        )
    return _result("keep", "title_taxonomy_candidate", signals, matched + extras, negatives)


def _result(action, reason, signals=None, matched=None, negatives=None,
            fallback=False, seed=False, relevant=False):
    return RelevanceResult(
        is_taxonomy_relevant=relevant,
        is_trend_seed=seed,
        filter_action=action,          # keep | discard
        filter_reason=reason,
        risk_signals=signals or [],
        matched_keywords=matched or [],
        negative_contexts=negatives or [],
        needs_comment_fallback=fallback,
    )
