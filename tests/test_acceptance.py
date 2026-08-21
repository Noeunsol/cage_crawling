"""2차 acceptance gate 불변식.

OpenAI 본문 분류를 제거한 대신 이 게이트가 저장 여부를 결정한다. 여기서 지키는 것:
  - 한국어로 쓰인 해외 사건은 domestic_direct가 아니다
  - 한국 관련이어도 목표 LV2 신호가 없으면 저장하지 않는다
  - 목표 Type이 달라도 같은 LV2 신호면 저장한다 (Type은 최종 라벨이 아니다)
"""
from datetime import date

import pytest

from src.phase2 import acceptance
from src.common.schema import ContentRecord, UrlCandidate
from src.common.site_registry import SiteRegistry

POLICY = {"require_domestic_direct": True, "require_lv2_evidence": True,
          "min_body_chars": 200, "min_korea_score": 0.60}
STRATEGY = {
    "recency_days": 365,
    "include_by_type": {
        "chemical": ["유해화학물질", "누출"],
        "explosive": ["폭발물", "불발탄"],
    },
}
TODAY = date(2026, 8, 18)


def _rec(title: str, body: str, url: str = "https://www.yna.co.kr/view/1",
         published_at: str | None = "2026-08-10") -> ContentRecord:
    return ContentRecord(
        source_url=url, domain="yna.co.kr", site_name="yna", site_type="news",
        taxonomy_lv2_candidate="6_O_CBRNE", subtype_candidate="chemical",
        title=title, body_text=body, collected_at="2026-08-18",
        search_query="q", search_api="serpapi", extractor="trafilatura",
        published_at=published_at,
    )


def _cand(target_type: str = "chemical", expected_lv2=None, expected_korea=None) -> UrlCandidate:
    return UrlCandidate(
        source_url="https://www.yna.co.kr/view/1", domain="yna.co.kr", search_query="q",
        search_api="serpapi", taxonomy_lv2_candidate="6_O_CBRNE", subtype_candidate=target_type,
        target_type=target_type,
        expected_lv2_evidence=expected_lv2 or ["유해화학물질", "누출"],
        expected_korea_evidence=expected_korea or ["환경부"],
    )


def _evaluate(rec, cand=None, strategy=None):
    return acceptance.evaluate(rec, cand or _cand(), strategy or STRATEGY, POLICY, TODAY)


_INCIDENT = (
    "환경부와 소방청은 경기도 화성시 사업장에서 유해화학물질이 누출되는 사고가 발생해 "
    "주민 대피와 함께 조사에 착수했다고 밝혔다. 소방 당국은 누출된 물질의 종류를 확인하고 "
    "인근 주민 피해 여부를 조사 중이라고 설명했다. 경찰도 사업장 관리 책임에 대한 수사에 나섰다. "
) * 2


def test_korean_language_overseas_event_is_not_domestic_direct():
    body = ("일본 후쿠시마 원전 인근 공장에서 유해화학물질이 누출되는 사고가 발생했다고 "
            "현지 언론이 보도했다. 현지 당국은 누출 규모를 조사하고 있다. ") * 4
    result = _evaluate(_rec("후쿠시마 화학물질 누출 사고", body, url="https://example.com/a"))
    assert not result.accepted
    assert result.reason.startswith(acceptance.HARD_REJECTS + ("not_domestic_direct",))


def test_domestic_incident_with_institution_and_location_passes():
    result = _evaluate(_rec("화성시 유해화학물질 누출 사고", _INCIDENT))
    assert result.accepted
    assert result.korea_evidence and result.lv2_evidence


def test_official_korean_agency_source_satisfies_korea_condition():
    body = ("사업장에서 유해화학물질 누출 사고가 발생해 관계 기관이 합동 조사에 착수했다. "
            "현장 통제와 함께 피해 여부를 확인하고 있다. ") * 5
    result = _evaluate(_rec("화학물질 누출 사고 대응", body, url="https://www.nfa.go.kr/notice/1"))
    assert result.accepted
    assert "한국 공식기관 출처" in result.korea_evidence


def test_korea_related_without_lv2_evidence_is_rejected():
    body = ("서울시와 경찰은 지역 축제 현장에서 발생한 안전 사고에 대해 조사에 착수했다고 밝혔다. "
            "주민 피해 여부를 확인하고 있다. ") * 5
    cand = _cand(expected_lv2=["유해화학물질", "누출"])
    strategy = {**STRATEGY, "include_by_type": {"chemical": ["유해화학물질", "누출"]}}
    result = _evaluate(_rec("서울 축제 안전 사고 조사", body), cand, strategy)
    assert not result.accepted
    assert result.reason == "missing_lv2_evidence"


def test_lv2_evidence_without_korea_relevance_is_rejected():
    body = ("A chemical leak occurred at a plant and investigators are reviewing the incident. "
            "유해화학물질 누출 정의와 일반적인 대응 절차를 설명한다. ") * 5
    result = _evaluate(_rec("유해화학물질 누출 개요", body, url="https://example.com/a"))
    assert not result.accepted
    assert result.reason.startswith(acceptance.HARD_REJECTS + ("not_domestic_direct",))


def test_different_target_type_but_same_lv2_evidence_passes():
    """목표 Type이 explosive여도 chemical 신호가 잡히면 같은 LV2이므로 통과한다."""
    cand = _cand(target_type="explosive", expected_lv2=["폭발물"])
    result = _evaluate(_rec("화성시 유해화학물질 누출 사고", _INCIDENT), cand)
    assert result.accepted
    assert "누출" in result.lv2_evidence


def test_stale_content_is_rejected_but_unknown_date_passes():
    stale = _rec("화성시 유해화학물질 누출 사고", _INCIDENT, published_at="2024-01-01")
    assert _evaluate(stale).reason == "stale:960d"

    undated = _rec("화성시 유해화학물질 누출 사고", _INCIDENT, published_at=None)
    assert _evaluate(undated).accepted


def test_short_or_definition_only_body_is_rejected():
    assert _evaluate(_rec("경기도 화성시 누출", "환경부 유해화학물질 누출 사고 조사")).reason.startswith(
        "low_localization_value:short")

    definition = ("환경부 고시에 따른 유해화학물질의 누출 기준과 국내 관리 체계를 정의한다. "
                  "관련 용어의 범위를 설명한다. ") * 5
    assert _evaluate(_rec("유해화학물질 누출 기준 정의", definition)).reason == (
        "low_localization_value:no_incident_context")


def test_toxic_news_requires_date_and_online_context():
    strategy = {
        "recency_days": 365,
        "include_by_type": {"profanity_and_insults": ["악플", "욕설"]},
        "acceptance": {
            "require_published_at": True,
            "required_context_terms": ["온라인", "댓글", "SNS"],
            "min_body_chars": 200,
        },
    }
    cand = _cand(target_type="profanity_and_insults", expected_lv2=["악플", "욕설"])
    body = ("서울 경찰은 온라인 댓글에 악플과 욕설을 반복 게시한 사건을 수사하고 "
            "피해자의 고소에 따라 피의자를 기소했다고 밝혔다. ") * 4
    assert _evaluate(_rec("온라인 악플 사건 기소", body), cand, strategy).accepted
    assert _evaluate(_rec("온라인 악플 사건 기소", body, published_at=None), cand, strategy).reason == (
        "missing_published_at")
    offline = body.replace("온라인", "직장").replace("댓글", "대화")
    assert _evaluate(_rec("악플 사건 기소", offline), cand, strategy).reason.startswith(
        "missing_required_context")


# ── 원문 수집형 LV2(4_I 등) 판정: 뉴스용 기준을 그대로 쓰면 정답을 못 모은다 ──
# 1_A는 이후 '뉴스 사건형'으로 전환돼 이 경로를 쓰지 않는다(korean_platform_context 미사용).
COMMUNITY_STRATEGY = {
    "recency_days": 90,
    "acceptance": {"korea_evidence": ["korean_platform_context", "korean_organization"],
                   "min_body_chars": 80, "require_incident_context": False},
    "lv2_risk_signals": ["toxic_language"],   # 실제 표현 어휘 경로
    "include_by_type": {"profanity_and_insults": ["온라인 모욕", "모욕죄"]},
}
_REGISTRY = SiteRegistry.load("configs/site_policy.yaml")


def _community_rec(title, body, domain="gall.dcinside.com"):
    return ContentRecord(
        source_url=f"https://{domain}/board/view/?no=1", domain=domain, site_name="dcinside",
        site_type="community", taxonomy_lv2_candidate="4_I_Privacy_Infringement",
        subtype_candidate="privacy_violation", title=title, body_text=body,
        collected_at="2026-08-19", search_query="q", search_api="board_list",
        extractor="dcinside", published_at="2026-08-15")


def _eval_community(rec):
    cand = UrlCandidate(rec.source_url, rec.domain, "q", "board_list",
                        "4_I_Privacy_Infringement", "privacy_violation",
                        target_type="profanity_and_insults")
    return acceptance.evaluate(rec, cand, COMMUNITY_STRATEGY, POLICY, TODAY, _REGISTRY)


def test_korean_platform_alone_proves_domestic_for_raw_expression():
    """커뮤니티 원문에 지명·기관이 나올 리 없다. 플랫폼 자체가 한국 맥락이다."""
    rec = _community_rec("숲음갤 병신인것도 맞는데", "이 새끼들 진짜 병신같다 " * 6)
    result = _eval_community(rec)
    assert result.accepted, result.reason
    assert any("한국 플랫폼 원문" in e for e in result.korea_evidence)
    assert "병신" in result.lv2_evidence      # risk_signals의 실제 표현 어휘로 잡힌다


def test_platform_context_does_not_apply_to_unregistered_domain():
    rec = _community_rec("병신같은 글", "씨발 " * 40, domain="example.com")
    assert not _eval_community(rec).accepted


def test_community_body_floor_matches_quality_filter():
    """quality가 커뮤니티 80자를 통과시키는데 acceptance가 200자를 요구하면 헛fetch가 된다."""
    short = _community_rec("씨발 뭐냐 이거", "아 진짜 씨발 개새끼들 뭐하는 짓이냐 이게 " * 5)
    assert len(short.body_text) >= 80 and len(short.body_text) < 200
    assert _eval_community(short).accepted


def test_incident_context_still_required_where_it_is_not_turned_off():
    """끄지 않은 LV2(뉴스형)에서는 사건 어휘 요구가 그대로 살아 있어야 한다."""
    strategy = {**COMMUNITY_STRATEGY, "acceptance": {
        **COMMUNITY_STRATEGY["acceptance"], "require_incident_context": True}}
    rec = _community_rec("병신 논쟁", "씨발 개새끼 " * 30)
    cand = UrlCandidate(rec.source_url, rec.domain, "q", "board_list",
                        "4_I_Privacy_Infringement", "privacy_violation")
    assert not acceptance.evaluate(rec, cand, strategy, POLICY, TODAY, _REGISTRY).accepted


@pytest.mark.parametrize("published_at,days", [
    ("2026-08-12", 6), ("2026-8-12", 6), ("2026.8.5", 13),
    ("2026-08-12 10:30:47", 6), ("몇 시간 전", None), ("", None),
])
def test_date_parsing_accepts_unpadded_korean_formats(published_at, days):
    """0 패딩 없는 '2026-8-12'를 못 읽으면 최근성 검사가 조용히 무력화된다."""
    assert acceptance._age_days(published_at, TODAY) == days


def test_weak_korea_evidence_is_collected_but_zero_is_not():
    """한국 근거로 거르진 않되, 근거가 0이면 받지 않는다.

    점수 0 = 지명·기관·제도가 본문에 하나도 없다는 뜻이다. 인코딩이 깨진 국내 기사도
    여기로 떨어지는데(한글 비율 0), 그건 fetcher가 charset을 고쳐서 살려야지
    이 게이트가 통과시킬 일이 아니다.
    """
    strategy = {**STRATEGY, "acceptance": {"require_domestic_direct": False,
                                           "require_incident_context": False}}
    weak = ("경찰은 유해화학물질 누출 신고를 접수해 조사에 착수했다고 밝혔다. "
            "현장 통제와 피해 확인이 진행 중이다. ") * 6
    result = _evaluate(_rec("유해화학물질 누출 신고 조사", weak, url="https://example.com/a"),
                       strategy=strategy)
    assert result.accepted, result.reason          # 임계 미만이어도 수집한다
    assert 0 < result.korea_score < 0.6            # 다만 점수는 그대로 남긴다

    none = ("A chemical leak occurred overseas and investigators are reviewing it. ") * 10
    zero = _evaluate(_rec("Overseas chemical leak", none, url="https://example.com/b"),
                     strategy=strategy)
    assert not zero.accepted and zero.reason.startswith("no_korea_context")


def test_domestic_flag_is_true_only_when_korea_evidence_is_real():
    result = _evaluate(_rec("화성시 유해화학물질 누출 사고", _INCIDENT))
    assert result.accepted and result.domestic is True and result.korea_score >= 0.6


# ── 표본 검수(2026-08-20)에서 드러난 오분류 두 종류 ──
def test_passing_mention_is_not_topic_evidence():
    """본문 어딘가에 한 번 나왔다고 그 taxonomy가 되지는 않는다.

    실측: 야구 기사 안의 "협박 메시지를 받은 동료" 한 문단, 카드사 유출 기사의
    "사이버 협박 보상 서비스" 상품 설명이 1회 매칭으로 1_A에 저장됐다.
    """
    strategy = {"recency_days": 365,
                "include_by_type": {"threats_and_intimidation": ["협박", "위협"]},
                "acceptance": {"min_lv2_hits": 2, "require_incident_context": False,
                               "require_domestic_direct": False}}
    cand = _cand(target_type="threats_and_intimidation", expected_lv2=["협박"])
    # 긴 기사에 한 번 스쳐 지나가는 언급 — 반복하면 그건 이미 주제다
    passing = ("삼성 구자욱은 가을야구 진출 소감을 밝혔다. 팀은 시즌 내내 부상과 부진을 "
               "견디며 순위를 끌어올렸고 팬들의 응원이 큰 힘이 됐다고 말했다. ") * 4 + \
        "동료 디아즈는 SNS에서 협박 메시지를 받은 적이 있다고 언급했다."
    assert _evaluate(_rec("구자욱 가을야구 소감", passing), cand, strategy).reason == (
        "missing_lv2_evidence")

    topical = ("경찰은 온라인에서 반복된 협박 게시글을 수사 중이라고 밝혔다. "
               "피의자는 협박과 위협을 반복해 피해자가 고소했다. ") * 4
    assert _evaluate(_rec("온라인 협박 사건 수사", topical), cand, strategy).accepted



def test_missing_lv2_evidence_is_not_overridable_by_permissive():
    """permissive는 '스니펫 사전필터·본문품질'을 끄는 것이지 taxonomy 정의를 끄는 게 아니다."""
    assert "missing_lv2_evidence" in acceptance.HARD_REJECTS


def test_missing_required_date_is_not_overridable_by_permissive():
    """날짜를 요구한 수집에서는 날짜 미상도 수집 기간 위반이다."""
    assert "missing_published_at" in acceptance.HARD_REJECTS


# ── 1_A 경계 확정(2026-08-20 표본검수 2차) ──
# 초점은 '언어 폭력'이다. 온라인/오프라인은 가리지 않되 말·글로 가한 가해가 있어야 하고,
# 딥페이크·디지털 성범죄는 1_B 소관이다.
def _toxic_strategy():
    import yaml
    from src.phase2 import run as gf
    p2 = yaml.safe_load(open("configs/targeted_collection.yaml", encoding="utf-8"))
    strategy = gf._merged_strategy("1_A_Toxic_Language", p2)
    by_id = {s["id"]: s for s in strategy["sources"]}
    return gf._source_strategy(strategy, by_id["web_news"]), p2["default_acceptance"]


def _news(title, body, url="https://www.yna.co.kr/view/1"):
    return ContentRecord(
        source_url=url, domain="yna.co.kr", site_name="yonhap_news", site_type="news",
        taxonomy_lv2_candidate="1_A_Toxic_Language", subtype_candidate="threats_and_intimidation",
        title=title, body_text=body, collected_at="2026-08-20", search_query="q",
        search_api="serpapi", extractor="trafilatura", published_at="2026-08-15")


def _toxic_eval(rec):
    strategy, policy = _toxic_strategy()
    cand = UrlCandidate(rec.source_url, rec.domain, "q", "serpapi", "1_A_Toxic_Language",
                        "threats_and_intimidation", target_type="threats_and_intimidation",
                        expected_lv2_evidence=["협박", "악플", "괴롭힘"])
    return acceptance.evaluate(rec, cand, strategy, policy, date(2026, 8, 20), _REGISTRY)


def test_physical_crime_leaves_no_toxic_evidence_to_filter_on():
    """1_A는 언어 폭력이다. 물리적 범죄만 다룬 기사에는 LV2 근거가 잡히지 않아야 한다.

    require_lv2_evidence를 끈 뒤(2026-08-21)로는 이런 기사도 저장된다. 그래서
    lv2_evidence가 비어 있다는 것이 나중에 걸러낼 유일한 단서다 — 이게 깨지면
    노이즈를 사후에 분리할 방법이 없어진다.
    """
    body = ("경찰은 교제 상대를 살해한 피의자를 구속했다고 밝혔다. 피해자는 위험도 A등급으로 "
            "분류됐으나 신변보호가 이뤄지지 않았다. 검찰은 구속영장을 청구했다. ") * 4
    result = _toxic_eval(_news("교제살인 피해자 영장 제외", body))
    assert result.lv2_evidence == [], result.lv2_evidence


def test_require_lv2_evidence_switch_still_rejects_when_turned_on():
    """스위치를 되살리면 근거 0인 후보는 permissive여도 다시 제외돼야 한다."""
    strategy, policy = _toxic_strategy()
    body = ("경찰은 교제 상대를 살해한 피의자를 구속했다고 밝혔다. 검찰은 구속영장을 청구했다. ") * 6
    rec = _news("교제살인 피해자 영장 제외", body)
    cand = UrlCandidate(rec.source_url, rec.domain, "q", "serpapi", "1_A_Toxic_Language",
                        "threats_and_intimidation", target_type="threats_and_intimidation",
                        expected_lv2_evidence=["협박", "악플", "괴롭힘"])
    result = acceptance.evaluate(rec, cand, strategy, {**policy, "require_lv2_evidence": True},
                                 date(2026, 8, 20), _REGISTRY)
    assert not result.accepted and result.reason == "missing_lv2_evidence"


def test_offline_harassment_with_verbal_abuse_stays_in_toxic_language():
    """온라인이 아니어도 폭언·욕설이 있으면 1_A다(직장 내 괴롭힘·태움)."""
    body = ("피해자는 상사의 폭언과 욕설이 반복됐다고 진술했다. 직장 내 괴롭힘 신고 이후에도 "
            "모욕적인 발언이 이어졌다고 밝혔다. 경찰은 사건을 종결했으나 검찰이 기소했다. ") * 4
    assert _toxic_eval(_news("직장내 괴롭힘 사건 기소", body)).accepted





def test_every_taxonomy_defining_reject_survives_permissive():
    """게이트를 새로 만들 때 HARD_REJECTS에 넣는 걸 잊으면 permissive가 그대로 뚫는다.

    실측(2026-08-20 2차 실험): off_topic_title·missing_required_context를 빠뜨려
    저장 14건 중 8건이 '게이트가 거부했는데 저장됨' 상태로 들어왔다.
    """
    defining = {"stale", "missing_published_at", "no_korea_context", "missing_lv2_evidence"}
    assert defining <= set(acceptance.HARD_REJECTS), defining - set(acceptance.HARD_REJECTS)
