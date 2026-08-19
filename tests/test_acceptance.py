"""2차 acceptance gate 불변식.

OpenAI 본문 분류를 제거한 대신 이 게이트가 저장 여부를 결정한다. 여기서 지키는 것:
  - 한국어로 쓰인 해외 사건은 domestic_direct가 아니다
  - 한국 관련이어도 목표 LV2 신호가 없으면 저장하지 않는다
  - 목표 Type이 달라도 같은 LV2 신호면 저장한다 (Type은 최종 라벨이 아니다)
"""
from datetime import date

from src.phase2 import acceptance
from src.schema import ContentRecord, UrlCandidate

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
    assert result.reason.startswith("not_domestic_direct")


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
    assert result.reason.startswith("not_domestic_direct")


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
