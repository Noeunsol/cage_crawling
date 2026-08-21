"""2차 targeted 저장 게이트 — OpenAI 본문 분류 대신 쓰는 결정론적 검증.

저장 조건은 네 가지다.
  1) domestic_direct  : 한국에서 벌어졌거나 한국의 피해·대응이 있는 사건인가
  2) 목표 LV2 evidence: 목표 LV2의 행위 신호가 본문에 실제로 있는가
                        (기본 off — require_lv2_evidence로 켠다. 꺼도 근거는 계산해
                         lv2_evidence에 남기므로 사후에 걸러낼 수 있다)
  3) recency          : LV2별 recency_days 안의 콘텐츠인가 (날짜 미상은 통과)
  4) 구체 맥락        : 정의·안내문이 아니라 구체 사건인가

통과한 레코드의 taxonomy_lv2는 "모델이 예측한 값"이 아니라
"검색 목표 LV2가 이 게이트를 통과해 확정된 값"이다. gap_filling이 그 provenance를
classification_source="targeted_acceptance_gate"로 기록한다.

Type은 최종 라벨이 아니므로 목표 Type과 다른 Type 신호가 잡혀도 같은 LV2면 통과시킨다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from src.common.filtering.korea_context import score_korea_context
from src.common.filtering.risk_signals import RISK_SIGNAL_KEYWORDS
from src.phase2.reranker import _term_hits

# 정의·안내문이 아니라 실제 사건임을 보이는 최소 어휘. 하나라도 없으면 로컬라이제이션 가치가 낮다.
_CONCRETE_TERMS = (
    "피해", "사건", "사고", "신고", "고소", "고발", "수사", "조사", "검거", "기소",
    "판결", "처벌", "논란", "적발", "발생", "당했", "겪었", "상담", "문의", "질문",
)
# 공식 한국 기관 도메인은 소스만으로 domestic_direct 근거가 된다.
_OFFICIAL_SUFFIXES = (".go.kr", ".or.kr")
# 한국 커뮤니티·Q&A 플랫폼의 글은 그 자체가 한국 이용자 맥락이다.
# score_korea_context는 지명·기관·피해·대응을 세는 뉴스·공문서용 점수기라
# 지식인 상담글·커뮤니티 원문은 0.00으로 떨어진다. 원문 수집형 LV2(4_I 등)가
# acceptance.korea_evidence에 korean_platform_context를 넣어 이 경로를 쓴다.
_PLATFORM_SITE_TYPES = {"community", "qna"}

# permissive_collection(=다 모으기)으로도 뚫리지 않는 사유.
# 수집 범위(기간)·최소 자격(한국 근거)·taxonomy 정의(LV2 근거)라 '품질 완화'와 성격이 다르다.
# permissive는 "스니펫으로 미리 거르지 말고 본문 품질도 따지지 말자"는 뜻이지,
# "이 taxonomy가 아니어도 저장하자"는 뜻이 아니다.
# 실측(2026-08-20 표본검수): 노이즈 8건 중 4건이 lv2 근거 0인데 permissive로 저장됐다.
HARD_REJECTS = ("stale", "missing_published_at", "no_korea_context", "missing_lv2_evidence",
                "off_topic_title", "missing_required_context", "not_news_source")


@dataclass
class AcceptanceResult:
    accepted: bool
    reason: str
    korea_evidence: list[str] = field(default_factory=list)
    lv2_evidence: list[str] = field(default_factory=list)
    # require_domestic_direct를 끄면 한국 관련성은 거르지 않고 점수·판정만 실어 보낸다.
    # 나중에 저장분에서 걸러낼 수 있도록 근거를 남기는 게 목적이다.
    domestic: bool = False
    korea_score: float = 0.0


def _reject(reason: str) -> AcceptanceResult:
    return AcceptanceResult(False, reason)


def _body_of(rec) -> str:
    return rec.core_text or rec.body_text or ""


def _official_source(url: str) -> bool:
    host = (url or "").split("//")[-1].split("/")[0].lower()
    return any(host.endswith(suffix) for suffix in _OFFICIAL_SUFFIXES)


def _korean_platform(rec, registry) -> bool:
    """site_policy에 등록된 한국 커뮤니티·Q&A 도메인인가."""
    if registry is None:
        return False
    site = registry.lookup((rec.domain or "").lower())
    return getattr(site, "site_name", "unknown") != "unknown" and \
        getattr(site, "site_type", "") in _PLATFORM_SITE_TYPES


def _korean_news_source(rec, registry) -> bool:
    """site_policy에 한국 뉴스 매체로 등록된 도메인인가."""
    if registry is None:
        return False
    site = registry.lookup((rec.domain or "").lower())
    return getattr(site, "site_name", "unknown") != "unknown" and \
        getattr(site, "site_type", "") == "news"


def _lv2_terms(candidate, strategy: dict) -> list[str]:
    """계획된 expected_lv2_evidence + 전략의 type별 가점 어휘 + 실제 유해 표현 어휘.

    include_by_type은 '사이버불링·모욕죄' 같은 보도·상담용 메타 어휘라
    당사자가 쓴 원문에는 하나도 걸리지 않는다. 원문 수집형 LV2는
    lv2_risk_signals로 risk_signals.py의 실제 표현 어휘를 더한다.
    """
    terms = list(getattr(candidate, "expected_lv2_evidence", []) or [])
    for signal in strategy.get("lv2_risk_signals") or []:
        terms += RISK_SIGNAL_KEYWORDS.get(signal, [])
    by_type = strategy.get("include_by_type") or {}
    target_type = getattr(candidate, "target_type", "")
    # 목표 Type 어휘를 앞에 두되, Type은 최종 라벨이 아니므로 같은 LV2의 다른 Type 신호도 인정한다.
    terms += by_type.get(target_type) or []
    for name, values in by_type.items():
        if name != target_type:
            terms += values
    terms += strategy.get("include") or []
    return list(dict.fromkeys(terms))


def _age_days(published_at: str | None, today: date) -> int | None:
    """YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD 및 뒤에 시각이 붙은 형태를 모두 받는다."""
    if not published_at:
        return None
    parts = published_at.strip().replace(".", "-").replace("/", "-")[:10].split("-")[:3]
    if len(parts) < 3:
        return None
    try:   # '2026-8-12'처럼 0 패딩이 없는 형식도 받는다(지식인 등)
        return (today - date(int(parts[0]), int(parts[1]), int(parts[2][:2]))).days
    except ValueError:
        return None


def evaluate(rec, candidate, strategy: dict, policy: dict,
             today: date | None = None, registry=None) -> AcceptanceResult:
    """strategy = source_strategies_by_lv2[lv2], policy = default_acceptance.

    한국 관련성 인정 방식은 전략의 acceptance.korea_evidence가 정한다.
      korean_organization / korean_location : score_korea_context 임계 (뉴스·공문서)
      korean_platform_context               : 등록된 한국 커뮤니티·Q&A 도메인 (원문 수집)
    공식기관(.go.kr/.or.kr)은 모드와 무관하게 항상 인정한다.
    """
    today = today or date.today()
    body = _body_of(rec)
    accept = {**policy, **(strategy.get("acceptance") or {})}
    modes = set(accept.get("korea_evidence") or ["korean_organization", "korean_location"])

    # [0] 제목이 다른 LV2의 주제이거나 마케팅 페이지면 본문을 볼 것도 없다.
    # 가장 먼저 본다 — 뒤에 두면 소관이 다른 문서가 '근거 없음'으로 기록돼 원인을 못 읽는다.
    # 실측(2026-08-20): 딥페이크·디지털성범죄 기사 3건이 '괴롭힘' 어휘로 1_A에 들어왔고,
    # 로펌 소개 페이지가 모욕·명예훼손 설명 때문에 만점 매칭됐다.
    off_topic = _term_hits(rec.title, accept.get("exclude_title_terms") or [])
    if off_topic:
        return _reject(f"off_topic_title:{off_topic[0]}")
    if accept.get("require_news_source") and not _korean_news_source(rec, registry):
        return _reject(f"not_news_source:{rec.domain}")

    # [1] domestic_direct
    korea_score, korea_evidence = score_korea_context(rec.title, body, rec.source_url)
    domestic = _official_source(rec.source_url)
    if domestic:
        korea_evidence = ["한국 공식기관 출처", *korea_evidence]
    if not domestic and _korean_news_source(rec, registry):
        domestic = True
        korea_evidence = [f"한국 뉴스 출처:{rec.site_name}", *korea_evidence]
    if not domestic and "korean_platform_context" in modes and _korean_platform(rec, registry):
        domestic = True
        korea_evidence = [f"한국 플랫폼 원문:{rec.site_name}", *korea_evidence]
    if not domestic and modes & {"korean_organization", "korean_location", "korean_person"}:
        domestic = korea_score >= float(accept.get("min_korea_score", 0.6))
    korea_evidence += [
        f"계획 근거:{term}"
        for term in _term_hits(f"{rec.title} {body}", getattr(candidate, "expected_korea_evidence", []) or [])
    ]
    # 한국 근거로 거르지 않기로 했어도 '근거가 하나도 없는' 콘텐츠까지 받을 이유는 없다.
    # 다만 공식기관·한국 플랫폼으로 이미 domestic이 선 건은 예외다 — 커뮤니티 원문은
    # 지명·기관이 안 나와 점수가 0.00이지만 플랫폼 자체가 한국 맥락이다.
    if not domestic and korea_score <= float(accept.get("reject_at_or_below_korea_score", 0.0)):
        return _reject(f"no_korea_context:{korea_score}")
    if accept.get("require_domestic_direct", True) and not domestic:
        return _reject(f"not_domestic_direct:{korea_score}")

    # [2] 목표 LV2 evidence — 근거는 항상 계산해 결과에 실어 보낸다.
    # require_lv2_evidence가 켜져 있을 때만 저장을 막는다(기본 off, 2026-08-21).
    haystack = f"{rec.title} {body}"
    lv2_evidence = _term_hits(haystack, _lv2_terms(candidate, strategy))
    # 등장 '여부'만 보면 주제가 아니라 스쳐 지나간 언급도 통과한다. 실측(2026-08-20):
    # 야구 기사 안의 "협박 메시지를 받은 동료" 한 문단, 카드사 유출 기사의
    # "사이버 협박 보상 서비스" 상품 설명이 이렇게 1_A로 들어왔다.
    min_hits = int(accept.get("min_lv2_hits", 1))
    if min_hits > 1 and sum(haystack.count(term) for term in lv2_evidence) < min_hits:
        lv2_evidence = []
    if accept.get("require_lv2_evidence", True) and not lv2_evidence:
        return _reject("missing_lv2_evidence")

    # [3] recency — 날짜 미상은 기본 통과(커뮤니티 원문은 날짜가 없는 경우가 많다).
    # 사건 보도를 노리는 LV2는 require_published_at: true로 날짜를 강제한다.
    recency_days = strategy.get("recency_days")
    age = _age_days(rec.published_at, today)
    if accept.get("require_published_at") and age is None:
        return _reject("missing_published_at")
    if recency_days and age is not None and age > int(recency_days):
        return _reject(f"stale:{age}d")

    # [4] 구체 맥락
    # 사건 어휘 요구는 뉴스·보도 수집용이다. '표현 원문' 자체가 목적인 LV2는
    # require_incident_context: false로 끈다 — 유해 표현 여부는 [2]가 이미 본다.
    if len(body) < int(accept.get("min_body_chars", 200)):
        return _reject(f"low_localization_value:short:{len(body)}")
    if accept.get("require_incident_context", True) and not _term_hits(f"{rec.title} {body}", _CONCRETE_TERMS):
        return _reject("low_localization_value:no_incident_context")
    required_context = accept.get("required_context_terms") or []
    if required_context:
        # 등장 횟수까지 본다. 물리적 범죄 기사에도 '협박'은 한 번쯤 스친다.
        hits = _term_hits(f"{rec.title} {body}", required_context)
        total = sum(f"{rec.title} {body}".count(term) for term in hits)
        if total < int(accept.get("min_required_context_hits", 1)):
            return _reject(f"missing_required_context:{total}")

    return AcceptanceResult(True, "targeted_acceptance_gate", korea_evidence, lv2_evidence,
                            domestic=domestic, korea_score=korea_score)
