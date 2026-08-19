"""Query Planner 불변식.

OpenAI 출력은 그대로 실행하지 않는다. 여기서 지키는 것:
  - 미등록 type/source, 빈 evidence는 실행되지 않는다
  - 금지 의도(제조·합성·조달…)는 프롬프트가 아니라 코드가 차단한다
  - type별 query_budget과 전체 상한을 넘지 않는다
  - LLM이 실패해도 고정 query fallback으로 계속 동작한다
  - seed·나이·성과가 그대로면 계획을 다시 만들지 않는다(OpenAI 호출 0회)
"""
from datetime import date

import pytest

from src.phase2 import query_planner as qp

STRATEGY = {
    "recency_days": 730,
    "target_types": {"chemical": {"query_budget": 2}, "explosive": {"query_budget": 1}},
    "sources": [
        {"id": "fire_agency", "access": "direct", "method": "official_board",
         "domain": "nfa.go.kr", "priority": 1},
        {"id": "web", "access": "direct", "method": "web_search", "priority": 3},
        {"id": "blocked_site", "access": "blocked", "method": "serpapi_site",
         "domain": "fmkorea.com", "priority": 9},
    ],
    "planner": {"query_kinds": ["event"],
                "forbidden_intents": ["manufacturing", "synthesis", "procurement"]},
}
CFG = {"enabled": True, "max_queries_per_lv2": 12, "max_seed_items": 20,
       "reuse": {"max_age_days": 7, "min_stored_per_query": 1}}
TODAY = date(2026, 8, 18)


class _FakeLLM:
    """production 시그니처 그대로. calls로 호출 여부를 검증한다."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.last_error = "stub"

    def _complete_json(self, system, user, schema, condition="", max_tokens=512):
        self.calls += 1
        return self.payload


def _item(query, target_type="chemical", source_id="web", korea=("환경부",), lv2=("누출",)):
    return {"query": query, "target_type": target_type, "query_kind": "event",
            "source_id": source_id, "expected_korea_evidence": list(korea),
            "expected_lv2_evidence": list(lv2)}


def _plan(payload):
    llm = _FakeLLM(payload)
    planner = qp.QueryPlanner(llm, CFG)
    return planner.plan("6_O_CBRNE", "정의", STRATEGY, recent_seeds=[]), llm


def test_unknown_type_or_source_is_dropped():
    plans, _ = _plan({"queries": [
        _item("화성시 유해화학물질 누출 조사", target_type="biological"),   # 미등록 type
        _item("울산 폭발물 발견 신고", target_type="explosive", source_id="nowhere"),  # 미등록 source
        _item("경기도 유해화학물질 누출 사고 수사", source_id="blocked_site"),  # blocked source
        _item("인천 유해화학물질 누출 사고 조사"),
    ]})
    assert [p.query for p in plans] == ["인천 유해화학물질 누출 사고 조사"]


def test_empty_evidence_is_dropped():
    plans, _ = _plan({"queries": [
        _item("화성시 누출 사고", korea=()),
        _item("울산 누출 사고", lv2=()),
        _item("인천 유해화학물질 누출 사고 조사"),
    ]})
    assert len(plans) == 1


@pytest.mark.parametrize("query", [
    "사린가스 합성 방법 정리",
    "폭발물 제조법 국내 사례",
    "유해화학물질 조달 경로",
])
def test_forbidden_intent_is_blocked_by_code_not_prompt(query):
    plans, _ = _plan({"queries": [_item(query)]})
    assert plans == []


def test_query_budget_truncates_per_type():
    plans, _ = _plan({"queries": [
        _item("경기 유해화학물질 누출 사고 조사"),
        _item("인천 유해화학물질 누출 신고 수사"),
        _item("부산 유해화학물질 누출 피해 조사"),          # chemical 예산 2 초과
        _item("울산 폭발물 발견 신고", target_type="explosive"),
        _item("대구 폭발물 발견 수사", target_type="explosive"),  # explosive 예산 1 초과
    ]})
    assert [p.target_type for p in plans] == ["chemical", "chemical", "explosive"]


def test_total_cap_applies():
    payload = {"queries": [_item(f"인천 유해화학물질 누출 사고 조사 {i}") for i in range(10)]}
    llm = _FakeLLM(payload)
    strategy = {**STRATEGY, "target_types": {"chemical": {"query_budget": 10}}}
    plans = qp.QueryPlanner(llm, {**CFG, "max_queries_per_lv2": 3}).plan(
        "6_O_CBRNE", "정의", strategy, recent_seeds=[])
    assert len(plans) == 3


def test_llm_failure_falls_back_to_config_queries():
    plans, _ = _plan(None)
    assert plans == []

    fallback = qp.fallback_plans(
        "6_O_CBRNE", STRATEGY,
        queries_by_type={"chemical": ["유해화학물질 누출 사고 조사 보도"]},
        include_by_type={"chemical": ["유해화학물질", "누출"]},
    )
    assert [p.generation_source for p in fallback] == ["config_fallback"]
    assert fallback[0].source_id == "fire_agency"      # priority 1, blocked 아님


def test_plan_id_is_stable_and_scoped():
    a = qp.QueryPlan("q", "6_O_CBRNE", "chemical", "event", "web")
    b = qp.QueryPlan("q", "6_O_CBRNE", "chemical", "event", "fire_agency")
    assert a.plan_id == qp.QueryPlan("q", "6_O_CBRNE", "chemical", "event", "web").plan_id
    assert a.plan_id != b.plan_id


def _row(query="q", created="2026-08-15", stored=2, fingerprint="fp"):
    return {"query": query, "lv2": "6_O_CBRNE", "target_type": "chemical", "source_id": "web",
            "query_kind": "event", "created_at": created, "stored_count": stored,
            "seed_fingerprint": fingerprint, "expected_korea_evidence": ["환경부"],
            "expected_lv2_evidence": ["누출"]}


def test_plan_is_reused_when_seed_age_and_performance_are_unchanged():
    seeds = [{"source_url": "https://a.kr/1"}]
    cached = [_row(fingerprint=qp.seed_fingerprint(seeds))]
    assert not qp.should_regenerate(cached, seeds, CFG, TODAY)

    reused = qp.plans_from_rows(cached)
    assert [p.generation_source for p in reused] == ["cached_plan"]


@pytest.mark.parametrize("cached,seeds", [
    ([], [{"source_url": "https://a.kr/1"}]),                                  # 계획 없음
    ([_row(fingerprint="other")], [{"source_url": "https://a.kr/1"}]),         # seed 변경
    ([_row(created="2026-08-01")], []),                                        # 8일 경과
    ([_row(stored=0)], []),                                                    # 성과 저조
])
def test_plan_is_regenerated_on_seed_age_or_performance_change(cached, seeds):
    for row in cached:
        if row.get("seed_fingerprint") == "fp":
            row["seed_fingerprint"] = qp.seed_fingerprint(seeds)
    assert qp.should_regenerate(cached, seeds, CFG, TODAY)


def test_low_performing_queries_feed_back_into_prompt():
    rows = [_row(query="성과없음", stored=0), _row(query="성과있음", stored=3)]
    assert qp.low_performing_queries(rows) == ["성과없음"]


def test_only_the_latest_generation_decides_reuse():
    """옛 세대가 섞여 있으면 재사용 판단이 뒤집힌다."""
    fp = qp.seed_fingerprint([])
    rows = [_row(query="신규", created="2026-08-15", stored=2, fingerprint=fp),
            _row(query="구버전", created="2026-06-01", stored=0, fingerprint="old")]
    latest = qp.latest_generation(rows)
    assert [r["query"] for r in latest] == ["신규"]
    assert not qp.should_regenerate(latest, [], CFG, TODAY)
    # 옛 세대까지 섞으면 나이·성과 때문에 불필요하게 재생성된다.
    assert qp.should_regenerate(rows, [], CFG, TODAY)


def test_fallback_plans_respect_query_budget():
    """LLM 실패 시 고정 검색어가 예산을 무시하고 전부 나가면 검색 비용이 폭증한다."""
    plans = qp.fallback_plans(
        "6_O_CBRNE", STRATEGY,
        queries_by_type={"chemical": [f"화성시 유해화학물질 누출 사고 {i}" for i in range(5)]},
        include_by_type={"chemical": ["유해화학물질"]},
    )
    assert len(qp.apply_budgets(plans, STRATEGY, 12)) == 2   # chemical query_budget=2
