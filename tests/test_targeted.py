"""2차 semantic targeted discovery 검증 (오프라인, assert 기반).

coverage/intent/provider/reranker 단위 + small_run end-to-end(provider/extract/LLM monkeypatch).
불변식: API content_hint는 본문 저장 금지, 추출 실패 시 미저장, korea 낮으면 excluded,
target/predicted 분리, provenance(run_id/phase=2), opportunistic 판정.
"""
import sqlite3
from collections import Counter
from datetime import date

import pytest
import yaml

from src.phase2 import run as pipeline
from src.phase2 import review as _review
from src.phase2 import coverage
from src.common.extract import ExtractorRouter
from src.common.extract.base import ExtractionOutcome
from src.common.classify import LLMMatcher
from src.phase2.intent_builder import (
    build_collection_intent,
    missing_manual_intents,
    validate_collection_intent,
)
from src.phase2.provider import MockTavilyProvider, SearchResult, SerpApiProvider, to_candidate
from src.phase2.reranker import rerank
from src.common.policy import load_policies
from src.common.schema import ContentRecord, MatchResult, UrlCandidate
from src.common.storage.store import Store
from src.common.site_registry import SiteRegistry

TAXO = "configs/taxonomy.yaml"
P2 = "configs/targeted_collection.yaml"
KBODY = ("특정인의 실명과 전화번호, 집 주소가 동의 없이 커뮤니티에 공개되어 큰 피해를 호소하고 있으며 "
         "고소가 가능한지 묻는 글이 계속 올라온다. 신상털이로 인한 2차 가해가 심각해 경찰에 신고했다는 "
         "댓글도 많다. 개인정보 유출 피해 사례가 반복되고 있어 대응 방법을 공유한다.") * 2


def _registry():
    return SiteRegistry.load("configs/site_policy.yaml")


# ── 단위: coverage ──
def test_weighted_coverage_and_deficits():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE content_records (taxonomy_lv2 TEXT, action TEXT, is_supplementary INT)")
    conn.executemany("INSERT INTO content_records VALUES (?,?,?)",
                     [("4_I", "accepted", 0), ("4_I", "review", 0), ("1_A", "accepted", 0)])
    cov = coverage.weighted_coverage_by_lv2(conn, 0.5)
    assert cov == {"4_I": 1.5, "1_A": 1.0}
    ranked = coverage.rank_deficits(cov, {"4_I": 30, "1_A": 1, "6_Q": 20})
    assert [r["lv2"] for r in ranked] == ["4_I", "6_Q"]   # 1_A 충분(deficit<0) 제외


def test_taxonomy_first_plan_keeps_taxonomy_yaml_order(tmp_path):
    db = tmp_path / "p2.db"
    Store(str(db)).close()
    rows = pipeline.preview_taxonomy_plan(P2, str(db))
    assert [row["lv2"] for row in rows] == [policy.taxonomy_lv2 for policy in load_policies(TAXO)]


def test_cbrne_plan_uses_tavily_then_site_scoped_serpapi(tmp_path):
    db = tmp_path / "p2.db"
    Store(str(db)).close()
    row = next(row for row in pipeline.preview_taxonomy_plan(P2, str(db))
               if row["lv2"] == "6_O_CBRNE")
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    policy = next(p for p in load_policies(TAXO) if p.taxonomy_lv2 == "6_O_CBRNE")
    intent = build_collection_intent(policy, cfg)

    assert row["provider_order"] == ["tavily", "serpapi"]
    # max_searches = type 수 × 2 → type별로 ①피해·상담 ②사건·보도 두 각도가 모두 나간다.
    assert len(intent.queries) == len(policy.subtypes) * 2 == 10
    assert set(intent.query_types.values()) == {st.name for st in policy.subtypes}
    assert all(any(k in query for k in ("한국", "국내", "대한민국"))
               for query in intent.queries)

    rule = cfg["serpapi"]["rules_by_lv2"]["6_O_CBRNE"]
    provider = SerpApiProvider(cfg["serpapi"]["provider"], {"6_O_CBRNE": rule})
    assert set(rule["domains_by_type"]) == {st.name for st in policy.subtypes}
    assert all(provider._domains(intent, query) for query in intent.queries)
    assert len(provider._search_terms(intent)) == len(policy.subtypes)


# ── 단위: intent (queries 그대로, exclude는 쿼리에서 분리, sensitive_overlay) ──
def _intent_cfg():
    return {
        "collection_intents_by_lv2": {"4_I_Privacy_Infringement": {
            "queries": ["신상털이 피해 고소 질문"], "include": ["신상털이"],
            "exclude": ["개인정보보호법 단순 설명"], "korea_required": True}},
        "sensitive_overlay": {"6_O_CBRNE": {
            "disallowed_intents": ["manufacturing"], "force_review": True}},
        "providers": {"tavily": {"max_results_per_query": 5}},
    }


def test_intent_builder_queries_and_sensitive_overlay():
    policies = {p.taxonomy_lv2: p for p in load_policies(TAXO)}
    cfg = _intent_cfg()
    m = build_collection_intent(policies["4_I_Privacy_Infringement"], cfg)
    assert m.queries == ["신상털이 피해 고소 질문"] and m.is_manual
    assert not any("4_I_Privacy" in q for q in m.queries)   # label을 검색어로 쓰지 않는다
    # exclude는 rerank 신호일 뿐 쿼리에 들어가지 않는다 (Tavily엔 부정 연산자가 없다)
    assert "개인정보보호법 단순 설명" in m.exclude
    assert not any("개인정보보호법" in q for q in m.queries)
    s = build_collection_intent(policies["6_O_CBRNE"], cfg)
    assert s.force_review and s.is_sensitive and any("manufacturing" in e for e in s.exclude)


def test_config_covers_every_taxonomy_lv2():
    """모든 LV2는 config에 수동 intent를 갖는다. 런타임은 경고만 하고 강제는 여기서 한다."""
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    missing = missing_manual_intents(load_policies(TAXO), cfg)
    assert not missing, f"{P2}에 수동 intent 누락: {missing}"


def test_collection_provider_routing_partitions_taxonomies():
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    routing = cfg["collection_provider_routing"]
    news = set(routing["news_tavily"]["lv2s"])
    community = set(routing["community_serpapi"]["lv2s"])
    all_lv2 = {policy.taxonomy_lv2 for policy in load_policies(TAXO)}
    assert not (news & community)
    assert news | community == all_lv2
    assert routing["news_tavily"]["provider"] == "tavily"
    assert routing["community_serpapi"]["provider"] == "serpapi_google"


def test_serpapi_rules_cover_every_taxonomy_with_domains():
    """SerpAPI 도입 시 특정 taxonomy만 도메인 규칙 없이 자동 검색으로 빠지는 것을 막는다."""
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    rules = cfg["serpapi"]["rules_by_lv2"]
    all_lv2 = {policy.taxonomy_lv2 for policy in load_policies(TAXO)}
    assert set(rules) == all_lv2
    assert all("domains" in rule or "domains_by_type" in rule for rule in rules.values())


def test_prohibited_advisory_serpapi_domains_are_split_by_type():
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))["serpapi"]
    rule = cfg["rules_by_lv2"]["3_H_Prohibited_Advisory"]
    assert set(rule["domains_by_type"]) == {"financial_advice", "legal_advice", "medical_advice"}
    # 지식인은 /qna 경로로 좁힌다 — 사이트 전체를 훑으면 태그·목록 페이지가 딸려 온다.
    assert "kin.naver.com/qna" in rule["domains_by_type"]["financial_advice"]
    # 로톡·닥터나우·하이닥은 '비교용 참고'가 아니라 주 수집원이다.
    assert "lawtalk.co.kr" in rule["domains_by_type"]["legal_advice"]
    assert "hidoc.co.kr" in rule["domains_by_type"]["medical_advice"]


def test_sensitive_lv2_queries_blocked_on_high_risk_terms():
    policies = {p.taxonomy_lv2: p for p in load_policies(TAXO)}
    cfg = _intent_cfg()
    cfg["collection_intents_by_lv2"]["6_O_CBRNE"] = {
        "queries": ["폭발물 제조 방법"], "include": ["사고"], "exclude": ["광고", "홍보"]}
    bad = build_collection_intent(policies["6_O_CBRNE"], cfg)
    assert validate_collection_intent(bad)["status"] == "blocked"
    # 실제 config의 민감 LV2는 통과해야 한다
    real = yaml.safe_load(open(P2, encoding="utf-8"))
    for lv2 in real["sensitive_overlay"]:
        intent = build_collection_intent(policies[lv2], real)
        assert validate_collection_intent(intent)["status"] != "blocked", lv2


def test_queries_by_type_splits_broad_lv2(caplog):
    """넓은 LV2는 type별 쿼리로 쪼갠다. 오타 type은 조용히 무시되지 않는다."""
    policies = {p.taxonomy_lv2: p for p in load_policies(TAXO)}
    cfg = _intent_cfg()
    cfg["collection_intents_by_lv2"]["2_F_Bias_and_Hate"] = {
        "max_searches": 3, "include": ["혐오"], "exclude": ["캠페인", "논문"],
        "queries": ["지역 비하 논란"],
        "queries_by_type": {"disability": ["장애인 비하 논란"], "gendr": ["오타 type"]},
    }
    with caplog.at_level("WARNING"):
        i = build_collection_intent(policies["2_F_Bias_and_Hate"], cfg)
    assert i.queries == ["지역 비하 논란", "장애인 비하 논란", "오타 type"]   # LV2 공통 + type별
    assert i.max_searches == 3                                          # LV2별 override
    assert "gendr" in caplog.text and "disability" not in caplog.text.split("없는 type")[1][:40]


def test_budget_cut_preserves_type_breadth():
    """상한에 걸려도 type을 통째로 버리지 않는다 — 라운드로빈이라 깊이만 줄어든다."""
    policies = {p.taxonomy_lv2: p for p in load_policies(TAXO)}
    cfg = _intent_cfg()
    cfg["collection_intents_by_lv2"]["2_F_Bias_and_Hate"] = {
        "max_searches": 3, "include": ["혐오"], "exclude": ["캠페인", "논문"],
        "queries_by_type": {
            "gender": ["성별 혐오 논란", "성별 혐오 보도", "성별 혐오 반응"],
            "disability": ["장애 비하 논란", "장애 비하 보도", "장애 비하 반응"],
            "religion": ["종교 비하 논란", "종교 비하 보도", "종교 비하 반응"],
        },
    }
    q = build_collection_intent(policies["2_F_Bias_and_Hate"], cfg).queries
    assert q == ["성별 혐오 논란", "장애 비하 논란", "종교 비하 논란"]   # 3 type 모두 1개씩


def test_config_queries_respect_configured_budget():
    """운영자가 LV2별 비용 상한을 낮춰도 실제 실행 쿼리는 상한을 넘지 않는다."""
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    for policy in load_policies(TAXO):
        intent = build_collection_intent(policy, cfg)
        assert len(intent.queries) <= intent.max_searches


def test_config_every_type_has_multiple_queries():
    """실제 config: 66개 type 전부 수동 쿼리를 갖고, 각도가 하나로 쏠리지 않는다."""
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))["collection_intents_by_lv2"]
    for policy in load_policies(TAXO):
        by_type = cfg[policy.taxonomy_lv2].get("queries_by_type") or {}
        assert set(by_type) == {st.name for st in policy.subtypes}, policy.taxonomy_lv2
        thin = [t for t, qs in by_type.items() if len(qs) < 2]
        assert not thin, f"{policy.taxonomy_lv2}: 쿼리 1개뿐인 type {thin}"


def test_queries_capped_by_search_budget():
    policies = {p.taxonomy_lv2: p for p in load_policies(TAXO)}
    cfg = _intent_cfg()
    cfg["providers"]["tavily"]["max_searches_per_lv2"] = 2
    cfg["collection_intents_by_lv2"]["4_I_Privacy_Infringement"]["queries"] = [
        "a 피해", "b 피해", "c 피해"]
    assert len(build_collection_intent(policies["4_I_Privacy_Infringement"], cfg).queries) == 2


def test_self_harm_queries_keep_target_type_provenance():
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    policy = next(p for p in load_policies(TAXO) if p.taxonomy_lv2 == "1_C_Self_Harm")
    intent = build_collection_intent(policy, cfg)
    assert len(intent.queries) == intent.max_searches
    assert set(intent.query_types.values()) == {"eating_disorder", "self_injury", "suicide"}
    assert set(intent.include_by_type) == set(intent.query_types.values())
    assert {"다이어트약", "거식증", "프로아나"} <= set(intent.include_by_type["eating_disorder"])
    assert "자해" in intent.include_by_type["self_injury"]
    assert {"자살", "극단적 선택"} <= set(intent.include_by_type["suicide"])


def test_self_harm_serpapi_uses_three_korean_qa_community_sites_for_one_year():
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    rule = cfg["serpapi"]["rules_by_lv2"]["1_C_Self_Harm"]
    expected = ["kin.naver.com/qna", "doctornow.co.kr", "instiz.net"]
    assert all(domains == expected for domains in rule["domains_by_type"].values())
    assert rule["max_domains_per_query"] == 3 and rule["tbs"] == "qdr:y"
    intent = build_collection_intent(
        next(policy for policy in load_policies(TAXO) if policy.taxonomy_lv2 == "1_C_Self_Harm"), cfg
    )
    self_injury_query = next(query for query, type_name in intent.query_types.items() if type_name == "self_injury")
    provider = SerpApiProvider(cfg["serpapi"]["provider"], {"1_C_Self_Harm": rule})
    assert provider._domains(intent, self_injury_query) == expected
    assert provider._search_terms(intent) == [
        (" ".join(group), type_name)
        for type_name, groups in rule["keyword_groups_by_type"].items()
        for group in groups
    ]


def test_serpapi_auth_error_does_not_expose_request_url_or_key():
    class FakeResponse:
        status_code = 401

    class FakeClient:
        def search(self, payload):
            error = Exception("401 Client Error: Unauthorized for url: https://serpapi.com/search?api_key=secret")
            error.response = FakeResponse()
            raise error

    with pytest.raises(RuntimeError, match="SerpAPI 인증 실패") as error:
        SerpApiProvider._request(FakeClient(), {"q": "test"})
    assert "secret" not in str(error.value)


def test_serpapi_wrapped_http_error_reports_status_without_exposing_key():
    class FakeResponse:
        status_code = 429

    class FakeClient:
        def search(self, payload):
            inner = Exception("request failed for api_key=secret")
            inner.response = FakeResponse()
            raise Exception(inner)

    with pytest.raises(RuntimeError, match="요청 한도 또는 크레딧") as error:
        SerpApiProvider._request(FakeClient(), {"q": "test"})
    assert "secret" not in str(error.value)


def test_global_excluded_domains_apply_to_every_intent():
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    policies = load_policies(TAXO)
    global_domains = set(cfg["providers"]["tavily"]["excluded_domains"])
    assert global_domains
    assert all(global_domains <= set(build_collection_intent(policy, cfg).excluded_domains)
               for policy in policies)


def test_robots_disallowed_domain_is_excluded_from_later_discovery(tmp_path):
    db = tmp_path / "p2.db"
    store = Store(str(db))
    store.save_candidate(UrlCandidate(source_url="https://blocked.example/article", domain="blocked.example",
                                      search_query="q", search_api="tavily",
                                      taxonomy_lv2_candidate="1_C_Self_Harm", subtype_candidate="",
                                      status="extraction_failed", filter_reason="robots_disallowed"))
    store.close()
    self_harm = next(row for row in pipeline.preview_intents(P2, str(db))
                     if row["lv2"] == "1_C_Self_Harm")
    assert "blocked.example" in self_harm["intent"].excluded_domains


# ── 단위: provider + to_candidate (content_hint는 후보에만) ──
def test_provider_standardizes_and_hint_on_candidate_only():
    r = SearchResult(provider="tavily", target_taxonomy_lv2="4_I_Privacy_Infringement",
                     query_or_intent="개인정보 피해", title="신상털이 피해", url="https://pann.nate.com/x",
                     snippet="스니펫", content_hint="힌트 본문", provider_score=0.8)
    cand = to_candidate(r, _registry())
    assert cand.collection_phase == 2 and cand.discovery_provider == "tavily"
    assert cand.content_hint == "힌트 본문" and cand.taxonomy_lv2_candidate == "4_I_Privacy_Infringement"
    assert not hasattr(cand, "body_text")


# ── 단위: reranker (rule로 명백 판정, LLM 미호출) ──
def test_reranker_rule_only_for_clear_cases():
    intent = build_collection_intent(
        {p.taxonomy_lv2: p for p in load_policies(TAXO)}["4_I_Privacy_Infringement"],
        {"providers": {"tavily": {}}})
    hi = SearchResult("tavily", "4_I_Privacy_Infringement", "q", "신상털이 개인정보 유출 피해",
                      "https://pann.nate.com/1", "신상 개인정보 유출", "신상털이 개인정보 유출", provider_score=0.9)
    assert rerank(hi, intent).source == "rule" and rerank(hi, intent).fetch_decision == "fetch"


def test_reranker_skips_non_korean_candidate_before_fetch():
    intent = build_collection_intent(
        {p.taxonomy_lv2: p for p in load_policies(TAXO)}["4_I_Privacy_Infringement"],
        {"providers": {"tavily": {}}})
    foreign = SearchResult("tavily", "4_I_Privacy_Infringement", "q", "privacy lawsuit",
                           "https://example.com/1", "personal data disclosure", "", provider_score=0.95)
    assert rerank(foreign, intent).fetch_decision == "skip"


def test_reranker_skips_candidate_below_relevance_floor():
    intent = build_collection_intent(
        {p.taxonomy_lv2: p for p in load_policies(TAXO)}["4_I_Privacy_Infringement"],
        {"providers": {"tavily": {}}})
    low = SearchResult("tavily", "4_I_Privacy_Infringement", "q", "모호한 한국 글",
                       "https://pann.nate.com/1", "관련 표현이 없는 짧은 글", "", provider_score=0.05)
    assert rerank(low, intent).fetch_decision == "skip"


def test_reranker_keeps_site_scoped_serpapi_without_exact_keyword_as_low_priority():
    intent = build_collection_intent(
        {p.taxonomy_lv2: p for p in load_policies(TAXO)}["4_I_Privacy_Infringement"],
        {"providers": {"tavily": {}}})
    irrelevant = SearchResult(
        "serpapi", "4_I_Privacy_Infringement", "site:kin.naver.com unrelated", "일반 질문",
        "https://kin.naver.com/qna/1", "일반 질문 본문", provider_score=1.0,
    )
    decision = rerank(irrelevant, intent)
    assert decision.fetch_decision == "low_priority"
    assert decision.discovery_relevance_score < 0.65


def test_reranker_still_skips_serpapi_with_clear_exclusion_signals():
    intent = build_collection_intent(
        {p.taxonomy_lv2: p for p in load_policies(TAXO)}["4_I_Privacy_Infringement"],
        {"providers": {"tavily": {}}})
    intent.exclude = ["광고", "개인정보처리방침"]
    excluded = SearchResult(
        "serpapi", "4_I_Privacy_Infringement", "site:kin.naver.com unrelated", "광고",
        "https://kin.naver.com/qna/1", "광고 개인정보처리방침", provider_score=1.0,
    )
    assert rerank(excluded, intent).fetch_decision == "skip"


def test_cbrne_incident_articles_pass_topic_and_event_gate():
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    policy = next(p for p in load_policies(TAXO) if p.taxonomy_lv2 == "6_O_CBRNE")
    intent = build_collection_intent(policy, cfg)
    cases = [
        ("chemical", "SK하이닉스 공장 화재로 유독가스 누출", "불소 가스가 퍼져 3600명이 대피했다"),
        ("chemical", "도심 집회에서 최루탄 노출 피해", "최루가스로 시민들이 다쳐 병원으로 이송됐다"),
        ("biological", "탄저균 등 생화학무기 대응", "서울시와 질병관리청이 생물테러대책반을 가동했다"),
        ("explosive", "총기 규제하는 한국, 옆집서 폭탄 만들고 있었다", "경찰이 사제 폭탄을 발견했다"),
        ("explosive", "하천에서 구형 고폭탄 발견", "군 폭발물처리반이 불발탄을 회수했다"),
        ("explosive", "훈련장 수류탄 폭발 사고", "장병들이 수류탄 폭발로 부상했다"),
        ("radiological", "한강에서 방사성 요오드 검출", "서울 하천에서 방사성물질이 검출됐다"),
        ("nuclear", "북한 핵물질 생산 관련 한국 정부 대응", "핵물질 생산 중단을 목표로 대응한다"),
    ]
    for type_name, title, snippet in cases:
        result = SearchResult(
            "serpapi", "6_O_CBRNE", "site:example", title,
            "https://www.police.go.kr/example", snippet, query_type=type_name,
        )
        decision = rerank(result, intent)
        assert decision.fetch_decision == "fetch", (type_name, decision)
        assert decision.discovery_relevance_score >= 0.65


def test_cbrne_general_administrative_document_stays_excluded():
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    policy = next(p for p in load_policies(TAXO) if p.taxonomy_lv2 == "6_O_CBRNE")
    intent = build_collection_intent(policy, cfg)
    result = SearchResult(
        "serpapi", "6_O_CBRNE", "site:nssc.go.kr", "새울 원자력발전소 운영허가(안)",
        "https://www.nssc.go.kr/example", "원자력 시설 정기 심의 자료", query_type="nuclear",
    )
    assert rerank(result, intent).fetch_decision == "skip"


# ── 단위: adjudicate (korea 게이트 + opportunistic) ──
def _adj_rec(korea, fit=0.9, concrete=0.9):
    r = ContentRecord(source_url="u", domain="d", site_name="s", site_type="community",
                      taxonomy_lv2_candidate="4_I", subtype_candidate="", title="t", body_text="b",
                      collected_at="2026", search_query="q", search_api="tavily", extractor="x")
    r.korea_relevance_score, r.taxonomy_fit_score, r.concrete_context_score = korea, fit, concrete
    r.is_harmful = True
    return r


def test_adjudicate_korea_gate_and_opportunistic():
    p2 = {"acceptance": {"min_taxonomy_fit_score": 0.75, "min_korea_relevance_score": 0.6,
                         "min_concrete_context_score": 0.6},
          "review": {"taxonomy_fit_score": 0.6, "korea_relevance_score": 0.4},
          "adjudication": {"allow_opportunistic_accept": True}}
    ok = MatchResult(is_relevant=True, taxonomy_lv2="4_I", subtype="privacy_violation",
                     confidence=0.9, reason="", source="llm")
    assert _review.adjudicate(ok, _adj_rec(0.9), "4_I", p2, lambda l: 5)[0] == "accepted"
    assert _review.adjudicate(ok, _adj_rec(0.3), "4_I", p2, lambda l: 5) == ("discard", "low_korea_relevance")
    # 불일치 + predicted 부족 → accepted(opportunistic)
    mm = MatchResult(is_relevant=True, taxonomy_lv2="2_F", subtype="gender", confidence=0.9, reason="", source="llm")
    assert _review.adjudicate(mm, _adj_rec(0.9), "4_I", p2, lambda l: 5)[0] == "accepted"
    # 불일치 + predicted 충분 → discard
    assert _review.adjudicate(mm, _adj_rec(0.9), "4_I", p2, lambda l: 0)[0] == "discard"


def test_broad_candidate_accepts_korean_harmful_near_match():
    p2 = {"acceptance": {"broad_candidate": True, "min_taxonomy_fit_score": 0.45,
                           "min_korea_relevance_score": 0.3, "min_concrete_context_score": 0.2}}
    match = MatchResult(is_relevant=True, taxonomy_lv2="4_I", subtype="privacy_violation",
                        confidence=0.48, reason="", source="llm")
    action, reason = _review.adjudicate(match, _adj_rec(0.4, 0.48, 0.25), "4_I", p2, lambda l: 0)
    assert (action, reason) == ("accepted", "broad_candidate")


# ── small_run end-to-end (오프라인 stub) ──
def _stub_classify(predicted, subtype="privacy_violation", korea=0.9, fit=0.85, concrete=0.8,
                   relevant=True, lv1="Information and Safety Harms"):
    def classify(self, rec, policies, valid_pairs=None, broad_candidate=False):
        rec.korea_relevance_score = korea
        rec.taxonomy_fit_score = rec.taxonomy_relevance_score = fit
        rec.concrete_context_score = concrete
        rec.harmfulness_score = 0.7
        rec.is_harmful = True
        rec.quality_score = 0.9
        return MatchResult(is_relevant=relevant, taxonomy_lv2=predicted, subtype=subtype,
                           confidence=0.9, reason="stub", taxonomy_lv1=lv1, source="llm")
    return classify


def _stub_extract(self, c, collected_at, task=None):
    rec = ContentRecord(source_url=c.source_url, domain=c.domain, site_name=c.site_name,
                        site_type=c.site_type, taxonomy_lv2_candidate=c.taxonomy_lv2_candidate,
                        subtype_candidate="", title=c.title or "제목", body_text=KBODY,
                        raw_text=KBODY, collected_at=collected_at, search_query=c.search_query,
                        search_api=c.search_api, extractor="stub")
    return ExtractionOutcome(record=rec, tried=["stub_ok"])


def _without_strategy(monkeypatch, lv2):
    """이 LV2만 고정 intent(레거시) 경로로 되돌린다.

    19개 LV2에 모두 전략이 생긴 뒤에도 레거시 경로는 코드에 살아 있으므로 계속 검증한다.
    """
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    p2["source_strategies_by_lv2"] = {k: v for k, v in p2["source_strategies_by_lv2"].items()
                                      if k != lv2}
    monkeypatch.setattr(_gf, "load_phase2_config", lambda path: p2)


def _run(tmp_path, monkeypatch, classify, extract=_stub_extract, lv2="3_G_Misinformation_and_Disinformation", verify=True):
    monkeypatch.setattr(ExtractorRouter, "extract", extract)
    monkeypatch.setattr(LLMMatcher, "classify", classify)
    _without_strategy(monkeypatch, lv2)
    db = tmp_path / "p2.db"
    rep = pipeline.small_run([lv2], limit=6, config_path=P2, db_path=str(db),
                             taxonomy_config=TAXO, provider=MockTavilyProvider(),
                             overrides={"adjudication": {"openai_verification": verify}})
    return db, rep


def test_small_run_accepted_provenance_and_hint_isolation(tmp_path, monkeypatch):
    db, rep = _run(tmp_path, monkeypatch, _stub_classify("3_G_Misinformation_and_Disinformation"))
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT taxonomy_lv2_candidate, taxonomy_lv2, action, collection_phase, run_id, "
        "discovery_provider, body_text FROM content_records WHERE action='accepted'").fetchall()
    assert rows, "accepted 최소 1건"
    tgt, pred, action, phase, run_id, prov, body = rows[0]
    assert tgt == "3_G_Misinformation_and_Disinformation" and pred == "3_G_Misinformation_and_Disinformation"
    assert phase == 2 and run_id and prov == "tavily"
    # content_hint(=discovery 메타)는 후보에만, 본문엔 없음
    hint = conn.execute("SELECT content_hint FROM url_candidates WHERE content_hint IS NOT NULL LIMIT 1").fetchone()
    assert hint and hint[0] and hint[0] not in body
    assert rep["provider_performance"]["tavily"]["accepted"] >= 1
    assert rep["mode"] == "targeted" and "coverage" in rep
    # 쿼리별 성과: 어느 검색어가 accepted를 만들었는지 다음 라운드 튜닝의 근거
    assert rep["by_query"] and all(q["query"] and q["lv2"] for q in rep["by_query"])
    assert sum(q["accepted"] for q in rep["by_query"]) == rep["provider_performance"]["tavily"]["accepted"]
    conn.close()


def test_small_run_without_openai_stores_targeted_tavily_accepted(tmp_path, monkeypatch):
    db, _ = _run(tmp_path, monkeypatch, _stub_classify("3_G_Misinformation_and_Disinformation"), verify=False)
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT action,classification_source FROM content_records LIMIT 1").fetchone()
    conn.close()
    assert row == ("accepted", "tavily_targeted")


def test_reverify_existing_tavily_candidate_with_openai(tmp_path, monkeypatch):
    db, rep = _run(tmp_path, monkeypatch, _stub_classify("3_G_Misinformation_and_Disinformation"), verify=False)
    result = _review.verify_unverified_candidates(
        rep["run_id"], str(db), P2, taxonomy_config=TAXO,
    )
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT action,classification_source FROM content_records WHERE run_id=? LIMIT 1", (rep["run_id"],)
    ).fetchone()
    status = conn.execute(
        "SELECT status FROM url_candidates WHERE run_id=? AND status='accepted' LIMIT 1", (rep["run_id"],)
    ).fetchone()
    conn.close()
    assert result["verified"] >= 1 and result["accepted"] >= 1
    assert row == ("accepted", "llm") and status == ("accepted",)


def test_permissive_small_run_keeps_low_korea_score_for_audit(tmp_path, monkeypatch):
    db, rep = _run(tmp_path, monkeypatch, _stub_classify("3_G_Misinformation_and_Disinformation", korea=0.2))
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT COUNT(*) FROM content_records WHERE action='accepted'").fetchone()[0]
    excluded = conn.execute(
        "SELECT COUNT(*) FROM url_candidates WHERE status='discard'").fetchone()[0]
    conn.close()
    assert stored >= 1 and excluded == 0


def test_small_run_extraction_failure_not_stored(tmp_path, monkeypatch):
    def fail_extract(self, c, collected_at, task=None):
        return ExtractionOutcome(record=None, tried=["all_fail"], reason="all_extractors_failed")
    db, rep = _run(tmp_path, monkeypatch, _stub_classify("3_G_Misinformation_and_Disinformation"), extract=fail_extract)
    conn = sqlite3.connect(db)
    content = conn.execute("SELECT COUNT(*) FROM content_records").fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM url_candidates WHERE status='extraction_failed'").fetchone()[0]
    conn.close()
    assert content == 0 and failed >= 1


def test_permissive_small_run_trusts_search_target_without_body_llm(tmp_path, monkeypatch):
    # permissive 2차는 본문 LLM 예측보다 검색 목표 taxonomy를 신뢰한다.
    db, rep = _run(tmp_path, monkeypatch,
                   _stub_classify("2_F_Bias_and_Hate", subtype="gender", lv1="Unfair Representation"))
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT taxonomy_lv2_candidate, taxonomy_lv2 FROM content_records WHERE action='accepted' LIMIT 1"
    ).fetchone()
    conn.close()
    assert row == ("3_G_Misinformation_and_Disinformation", "3_G_Misinformation_and_Disinformation")


def _urls(db):
    return {r[0] for r in sqlite3.connect(db).execute(
        "SELECT canonical_url FROM content_records WHERE canonical_url IS NOT NULL")}


def test_reference_db_blocks_urls_already_in_main_db(tmp_path, monkeypatch):
    """scratch DB로 돌려도 본 DB에 있는 URL은 다시 사오지 않는다(검색·fetch·분류 재지출 방지)."""
    main_db = tmp_path / "main.db"
    _run_to(main_db, monkeypatch)
    assert _urls(main_db), "본 DB에 먼저 수집돼 있어야 한다"

    # reference_db 없으면 같은 URL을 그대로 다시 수집한다 (구 동작)
    without = tmp_path / "without.db"
    _run_to(without, monkeypatch)
    assert _urls(without) & _urls(main_db), "재현 전제: 참조 DB 없이는 중복 수집된다"

    # reference_db를 주면 본 DB와 겹치는 URL이 없다
    with_ref = tmp_path / "with_ref.db"
    _run_to(with_ref, monkeypatch, reference_db=str(main_db))
    assert not (_urls(with_ref) & _urls(main_db)), "본 DB에 있는 URL을 scratch가 다시 수집함"


def _run_to(db, monkeypatch, **kw):
    monkeypatch.setattr(ExtractorRouter, "extract", _stub_extract)
    monkeypatch.setattr(LLMMatcher, "classify", _stub_classify("3_G_Misinformation_and_Disinformation"))
    _without_strategy(monkeypatch, "3_G_Misinformation_and_Disinformation")
    return pipeline.small_run(["3_G_Misinformation_and_Disinformation"], limit=6, config_path=P2,
                              db_path=str(db), taxonomy_config=TAXO,
                              provider=MockTavilyProvider(), **kw)


# ── 전략 경로(source_strategies_by_lv2) end-to-end ──
# 이 경로의 계약: OpenAI는 검색 계획에만 쓰고, 저장 여부는 결정론적 acceptance gate가 정한다.
from src.phase2 import run as _gf                       # noqa: E402
from src.phase2 import query_planner as qp                          # noqa: E402
from src.phase2.query_planner import QueryPlan, QueryPlanner       # noqa: E402

CBRNE_BODY = (
    "환경부와 소방청은 경기도 화성시 사업장에서 유해화학물질이 누출되는 사고가 발생해 주민 대피와 "
    "함께 조사에 착수했다고 밝혔다. 소방 당국은 누출 물질을 확인하고 인근 주민 피해 여부를 조사 중이다. "
    "경찰도 사업장 관리 책임에 대한 수사에 나섰다.") * 3


def test_news_homepage_is_not_an_article_candidate():
    assert _gf._non_article_url_reason("https://www.ytn.co.kr") == "homepage_url"
    assert _gf._non_article_url_reason("https://www.ytn.co.kr/") == "homepage_url"
    assert not _gf._non_article_url_reason("https://www.ytn.co.kr/_ln/0103_202606282356047019")


def _plan(source_id, query="화성시 유해화학물질 누출 사고 조사"):
    return QueryPlan(query=query, target_lv2="6_O_CBRNE", target_type="chemical",
                     query_kind="event", source_id=source_id,
                     expected_korea_evidence=["환경부"], expected_lv2_evidence=["유해화학물질", "누출"])


def _result(url, source_id, query="화성시 유해화학물질 누출 사고 조사"):
    return SearchResult(provider="serpapi", target_taxonomy_lv2="6_O_CBRNE", query_or_intent=query,
                        title="화성시 유해화학물질 누출 사고", url=url, snippet="스니펫 본문 아님",
                        content_hint="스니펫 본문 아님", query_type="chemical", provider_score=0.9, rank=1)


def _cbrne_strategy(**over):
    strategy = {
        "modes": ["official_seed", "search_expand"], "recency_days": 730,
        "target_types": {"chemical": {"query_budget": 3}},
        "sources": [
            {"id": "fire_agency", "access": "direct", "method": "official_board",
             "domain": "nfa.go.kr", "priority": 1, "store_seed": True},
            {"id": "web", "access": "direct", "method": "web_search", "priority": 3},
        ],
        "planner": {"query_kinds": ["event"], "forbidden_intents": ["manufacturing"]},
        "include_by_type": {"chemical": ["유해화학물질", "누출"]},
    }
    strategy.update(over)
    return strategy


def _stub_cbrne_extract(self, c, collected_at, task=None):
    # 후보마다 본문을 달리해야 simhash 근접중복에 걸리지 않는다.
    body = f"{c.title or ''} {CBRNE_BODY}"
    rec = ContentRecord(source_url=c.source_url, domain=c.domain, site_name=c.site_name,
                        site_type=c.site_type, taxonomy_lv2_candidate=c.taxonomy_lv2_candidate,
                        subtype_candidate="", title=c.title or "화성시 유해화학물질 누출 사고",
                        body_text=body, raw_text=body, collected_at=collected_at,
                        search_query=c.search_query, search_api=c.search_api, extractor="stub",
                        published_at="2026-08-10")
    return ExtractionOutcome(record=rec, tried=["stub_ok"])


def _run_strategy(tmp_path, monkeypatch, strategy=None, results_by_source=None,
                  extract=_stub_cbrne_extract, limit=6, overrides=None):
    """전략 경로 실행. planner와 source discovery만 갈아끼우고 나머지는 실제 코드를 탄다."""
    strategy = strategy or _cbrne_strategy()
    results_by_source = results_by_source or {
        "fire_agency": [_result("https://www.nfa.go.kr/notice/1", "fire_agency")],
        "web": [_result("https://www.yna.co.kr/view/1", "web")],
    }
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    p2["source_strategies_by_lv2"] = {"6_O_CBRNE": strategy}
    monkeypatch.setattr(_gf, "load_phase2_config", lambda path: p2)

    calls = {"plan": 0, "classify": 0, "extract": []}

    def fake_plan(self, lv2, definition, strat, seeds, low=None, query_kind="", max_queries=None):
        calls["plan"] += 1
        calls.setdefault("seeds", []).append(list(seeds))
        calls.setdefault("max_queries", []).append(max_queries)
        return [_plan(s["id"]) for s in strat["sources"]]

    def fake_discover(plans, source, strat, lv2, ctx):
        calls.setdefault("recency", []).append(strat.get("recency_days"))
        if source.get("access") == "blocked":
            return []
        return results_by_source.get(source["id"], []) if plans else []

    def counting_extract(self, c, collected_at, task=None):
        calls["extract"].append(c.source_url)
        return extract(self, c, collected_at, task)

    def counting_classify(self, *a, **kw):
        calls["classify"] += 1
        return None

    monkeypatch.setattr(QueryPlanner, "plan", fake_plan)
    monkeypatch.setattr(_gf._router, "discover", fake_discover)
    monkeypatch.setattr(ExtractorRouter, "extract", counting_extract)
    monkeypatch.setattr(LLMMatcher, "classify", counting_classify)
    db = tmp_path / "strategy.db"
    report = pipeline.small_run(["6_O_CBRNE"], limit=limit, config_path=P2, db_path=str(db),
                                taxonomy_config=TAXO, provider=MockTavilyProvider(),
                                overrides=overrides)
    return db, report, calls


def test_strategy_run_stores_with_acceptance_gate_and_never_classifies_body(tmp_path, monkeypatch):
    db, _, calls = _run_strategy(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT taxonomy_lv2, target_type, classification_source, korea_relevance_type, "
        "korea_evidence, lv2_evidence, source_id, query_plan_id FROM content_records").fetchall()
    conn.close()
    assert rows, "acceptance gate 통과 레코드 최소 1건"
    for lv2, target_type, source, korea_type, korea_ev, lv2_ev, source_id, plan_id in rows:
        assert lv2 == "6_O_CBRNE" and target_type == "chemical"
        assert source == "targeted_acceptance_gate" and korea_type == "domestic_direct"
        assert "누출" in lv2_ev and korea_ev != "[]" and source_id and plan_id
    # 본문 LLM 분류는 호출되지 않는다. OpenAI는 검색 계획에만 쓴다.
    assert calls["classify"] == 0 and calls["plan"] >= 1


def _body_extract(text, title):
    def _extract(self, c, collected_at, task=None):
        outcome = _stub_cbrne_extract(self, c, collected_at, task)
        outcome.record.title = title
        outcome.record.body_text = outcome.record.raw_text = text
        return outcome
    return _extract


def test_permissive_stores_weak_korea_evidence_but_not_zero(tmp_path, monkeypatch):
    """공통 기준은 뉴스-only가 아니라 한국어 + 한국 맥락이다."""
    weak = ("경찰은 유해화학물질 누출 신고를 접수해 조사에 착수했다고 밝혔다. ") * 8
    db, _, _ = _run_strategy(
        tmp_path, monkeypatch, extract=_body_extract(weak, "유해화학물질 누출 신고 조사"),
        results_by_source={"web": [_result("https://www.yna.co.kr/view/a", "web")]})
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT classification_source,classification_reason FROM content_records LIMIT 1"
    ).fetchone()
    conn.close()
    assert row and row[0] == "targeted_acceptance_gate"


def test_permissive_does_not_store_zero_korea_context(tmp_path, monkeypatch):
    """실측(2026-08-19): 인코딩이 깨진 국내 기사가 이 경로로 저장됐다."""
    none = "A chemical leak occurred overseas and investigators are reviewing it. " * 10
    db, _, _ = _run_strategy(
        tmp_path, monkeypatch, extract=_body_extract(none, "Overseas chemical leak"),
        results_by_source={"web": [_result("https://example.com/a", "web")]})
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT COUNT(*) FROM content_records").fetchone()[0]
    reason = conn.execute(
        "SELECT filter_reason FROM url_candidates WHERE status='quality_failed'").fetchone()
    conn.close()
    assert stored == 0, "한국 근거 0인 콘텐츠가 저장됐다"
    assert reason and reason[0].startswith("not_korean")


def test_discovery_only_source_is_never_fetched(tmp_path, monkeypatch):
    strategy = _cbrne_strategy(modes=["search_planned"], sources=[
        {"id": "web", "access": "discovery_only", "method": "web_search", "priority": 1}])
    db, _, calls = _run_strategy(tmp_path, monkeypatch, strategy=strategy,
                                 results_by_source={"web": [_result("https://example.com/a", "web")]})
    assert calls["extract"] == [], "discovery_only 후보를 fetch했다"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM content_records").fetchone()[0] == 0
    status, hint = conn.execute(
        "SELECT status, content_hint FROM url_candidates LIMIT 1").fetchone()
    conn.close()
    assert status == "discovery_only" and hint      # snippet은 후보에만 남는다


def test_blocked_source_is_not_called_at_all(tmp_path, monkeypatch):
    strategy = _cbrne_strategy(modes=["search_planned"], sources=[
        {"id": "web", "access": "blocked", "method": "web_search", "priority": 1}])
    db, _, calls = _run_strategy(tmp_path, monkeypatch, strategy=strategy)
    assert calls["extract"] == []
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM url_candidates").fetchone()[0] == 0
    conn.close()


def test_official_document_is_stored_and_seeds_the_followup_search(tmp_path, monkeypatch):
    db, _, calls = _run_strategy(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    official = conn.execute(
        "SELECT source_url FROM content_records WHERE is_official_seed=1").fetchall()
    conn.close()
    assert official and official[0][0].endswith("nfa.go.kr/notice/1")
    # 2라운드 계획은 1라운드에서 저장된 공식 문서를 seed로 받는다.
    assert calls["plan"] >= 2
    assert any(s["source_url"].startswith("https://www.nfa.go.kr")
               for s in calls["seeds"][-1]), "후속 검색이 공식 seed를 받지 못했다"


CITIES = ["화성시", "울산광역시", "여수시", "부산광역시", "인천광역시"]


def test_max_queries_is_the_only_credit_knob():
    """수집 목표는 부족분 랭킹용이고 검색을 막지 않는다.

    예전에는 '목표 - 이미 모은 수'로 살 검색어를 줄였는데, 목표를 채운 LV2는 검색어를
    0개 사서 아무 일도 없이 끝났다(화면엔 "완료"만 남아 오류로 읽혔다).
    이제 살 검색어 수는 max_queries_per_lv2 하나가 정한다.
    """
    assert qp.max_queries({"max_queries_per_lv2": 68}) == 68
    assert qp.max_queries({}) == 12                       # 기본값
    assert not hasattr(qp, "queries_needed"), "목표 기반 절감 로직이 되살아났다"


def test_collection_target_is_defined_in_exactly_one_place():
    """같은 숫자가 여러 곳에 흩어지면 반드시 어긋난다(실제로 어긋났었다)."""
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    assert "min_accepted_per_lv2" in p2["target_selection"]
    for lv2, plan in ((p2.get("taxonomy_collection_plan") or {}).get("plans") or {}).items():
        assert "target_count" not in plan, f"{lv2}: 목표가 plans에 중복 정의됐다"
    for lv2, strategy in p2["source_strategies_by_lv2"].items():
        assert "lv2_store_target" not in strategy, f"{lv2}: 목표가 전략에 중복 정의됐다"


def test_all_lv2_share_one_target():
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    targets = coverage.resolve_targets(load_policies(TAXO), p2["target_selection"])
    assert set(targets.values()) == {100.0}, targets


def test_paid_candidates_are_used_even_past_the_store_target(tmp_path, monkeypatch):
    # 근접중복에 걸리지 않도록 서로 다른 사건으로 만든다.
    results = []
    for i, city in enumerate(CITIES):
        r = _result(f"https://www.yna.co.kr/view/{i}", "web")
        r.title = f"{city} 유해화학물질 누출 사고"
        results.append(r)

    def city_extract(self, c, collected_at, task=None):
        outcome = _stub_cbrne_extract(self, c, collected_at, task)
        city = (c.title or "").split()[0]
        outcome.record.body_text = outcome.record.raw_text = (
            f"{city} 소재 사업장에서 유해화학물질 누출 사고가 발생해 {city} 소방본부와 환경부가 "
            f"현장을 통제하고 주민 피해를 조사했다. {city} 경찰도 관리 책임 수사에 착수했다. ") * 4
        return outcome

    strategy = _cbrne_strategy(modes=["search_planned"], sources=[
        {"id": "web", "access": "direct", "method": "web_search", "priority": 1}])
    db, _, calls = _run_strategy(tmp_path, monkeypatch, strategy=strategy,
                                 results_by_source={"web": results}, extract=city_extract)
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT COUNT(*) FROM content_records").fetchone()[0]
    conn.close()
    # 목표 2건을 넘겨도 이미 검색비를 치른 후보는 끝까지 쓴다.
    assert stored == len(CITIES), f"산 후보를 버렸다: {stored}/{len(CITIES)}건만 저장"
    assert len(calls["extract"]) == len(CITIES)


def test_query_plan_performance_is_recorded(tmp_path, monkeypatch):
    db, report, _ = _run_strategy(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT discovered_count, fetch_success_count, stored_count FROM query_plans").fetchall()
    conn.close()
    assert rows and any(d >= 1 and f >= 1 and s >= 1 for d, f, s in rows)
    # 리포트로도 나가야 사람이 검색어·source를 조정할 수 있다.
    assert report["query_plans"] and any(r["stored_count"] >= 1 for r in report["query_plans"])
    assert all(q.get("query_plan_id") and q.get("source_id") for q in report["by_query"])


def test_source_strategy_config_is_executable():
    """설정만 커지고 실행 불가한 상태를 막는다."""
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    types_by_lv2 = {p.taxonomy_lv2: {t.name for t in p.subtypes} for p in load_policies(TAXO)}
    known_methods = {"board_list", "official_board", "serpapi_site", "web_search"}
    for lv2, strategy in p2["source_strategies_by_lv2"].items():
        assert lv2 in types_by_lv2, f"{lv2}: taxonomy에 없는 LV2"
        assert set(strategy["target_types"]) <= types_by_lv2[lv2], f"{lv2}: 미등록 type"
        assert strategy["recency_days"] > 0
        for source in strategy["sources"]:
            assert source["method"] in known_methods, f"{lv2}/{source['id']}: 미지원 method"
            assert source["access"] in {"direct", "discovery_only", "metadata_only", "blocked"}
            if source["method"] in {"official_board", "serpapi_site"}:
                assert source.get("domain") or source.get("domains"), \
                    f"{lv2}/{source['id']}: site: 검색에 domain 필요"
            if source["method"] == "board_list":
                assert source.get("galleries"), f"{lv2}/{source['id']}: 게시판 목록 필요"


def test_taxonomy_plan_runs_strategy_lv2_once(tmp_path, monkeypatch):
    """전략 LV2를 type×provider로 쪼개면 계획만 반복 생성되고 같은 source를 중복 호출한다."""
    calls = []

    def fake_small_run(lv2s, limit, *a, **kw):
        calls.append((tuple(lv2s), kw.get("query_types")))
        return {"stored_records": 0, "run_id": "r"}

    monkeypatch.setattr(_gf, "small_run", fake_small_run)
    _gf.run_taxonomy_plan(["6_O_CBRNE"], P2, str(tmp_path / "plan.db"), taxonomy_config=TAXO)
    assert calls == [(("6_O_CBRNE",), None)]


def test_cached_plan_is_not_resaved_so_it_can_expire(tmp_path):
    """재사용할 때마다 저장하면 created_at이 갱신돼 max_age_days가 영원히 오지 않는다."""
    from datetime import date
    store = Store(str(tmp_path / "plans.db"))
    strategy = _cbrne_strategy(modes=["search_planned"])
    p2 = {"query_planner": {"max_seed_items": 20, "max_queries_per_lv2": 12,
                            "reuse": {"max_age_days": 7, "min_stored_per_query": 0}}}

    class _Planner:
        calls = 0

        def plan(self, *a, **kw):
            _Planner.calls += 1
            return [_plan("web")]

    planner = _Planner()
    day1, day8 = date(2026, 8, 1), date(2026, 8, 8)
    _gf._plans_for(store, "6_O_CBRNE", strategy, p2, planner, "정의", day1)
    assert _Planner.calls == 1

    # 3일 뒤: seed·성과 그대로 → 재사용, 저장하지 않음
    plans = _gf._plans_for(store, "6_O_CBRNE", strategy, p2, planner, "정의", date(2026, 8, 4))
    assert _Planner.calls == 1 and [p.generation_source for p in plans] == ["cached_plan"]
    assert {r["created_at"] for r in store.load_query_plans("6_O_CBRNE")} == {"2026-08-01"}

    # 8일 뒤: 만료 → 재생성
    _gf._plans_for(store, "6_O_CBRNE", strategy, p2, planner, "정의", day8)
    assert _Planner.calls == 2
    store.close()


# 본문을 가져올 수 없다고 실측된 도메인(2026-08). 검색 대상에 넣으면
# SerpAPI 크레딧만 쓰고 저장은 0건이 된다. Playwright·Firecrawl은 기본 off라
# "정적으로 안 되면 못 쓴다"가 실질 기준이다.
UNFETCHABLE_DOMAINS = (
    "fmkorea.com",     # robots: User-agent:* → Disallow:/
    "pann.nate.com",   # robots: 동일
    "velog.io",        # SPA — 정적 HTML에 본문 0자
    "blog.naver.com",  # iframe 구조 — 정적 HTML에 본문 0자
    "me.go.kr",        # 기후에너지환경부(mcee.go.kr)로 개편, 리디렉트 셸만 남음
    "chosun.com",      # SPA — 기사 본문이 정적 HTML에 없음
    "khan.co.kr",      # 우리 UA에 http_403
)

NON_TARGET_DOMAINS = ("github.com", "gist.github.com", "raw.githubusercontent.com")


def test_global_excluded_domains_cover_unfetchable_and_non_target_sources():
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    tavily = set(p2["providers"]["tavily"]["excluded_domains"])
    serpapi = set(p2["serpapi"]["provider"]["excluded_domains"])
    expected = set(UNFETCHABLE_DOMAINS + NON_TARGET_DOMAINS)
    assert expected <= tavily
    assert expected <= serpapi
    assert expected <= set(_gf._merged_strategy("1_A_Toxic_Language", p2)["excluded_domains"])


def test_search_domains_exclude_unfetchable_sites():
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    targets = []
    for lv2, rule in p2["serpapi"]["rules_by_lv2"].items():
        targets += [(lv2, d) for d in rule.get("domains") or []]
        for types in (rule.get("domains_by_type") or {}).values():
            targets += [(lv2, d) for d in types]
    for strategy in p2["source_strategies_by_lv2"].values():
        for src in strategy["sources"]:
            if src.get("access") != "direct":
                continue
            targets += [("strategy", d) for d in
                        (src.get("domains") or ([src["domain"]] if src.get("domain") else []))]
    offenders = [(lv2, d) for lv2, d in targets
                 if any(blocked in d for blocked in UNFETCHABLE_DOMAINS)]
    assert not offenders, f"본문 수집 불가 도메인이 검색 대상에 있음: {offenders}"


def test_append_terms_are_or_grouped_not_and_chained():
    """AND로 붙이면 세 어휘를 모두 가진 문서만 남아 결과가 사실상 0이 된다."""
    provider = SerpApiProvider({}, {"X": {"append_terms": ["유출", "공개", "내부정보"]}})
    intent = build_collection_intent(
        {p.taxonomy_lv2: p for p in load_policies(TAXO)}["4_J_Public_Sensitive_Info_Leakage"],
        yaml.safe_load(open(P2, encoding="utf-8")))
    intent.target_taxonomy_lv2 = "X"
    terms = provider._search_terms(intent)
    assert terms and all(t.endswith(" (유출 OR 공개 OR 내부정보)") for t, _ in terms)


def test_append_terms_by_type_overrides_lv2_common():
    rule = {"append_terms": ["공통"], "append_terms_by_type": {"legal_advice": ["고소", "소송"]}}
    provider = SerpApiProvider({}, {"X": rule})
    assert provider._append_terms(rule, "legal_advice") == " (고소 OR 소송)"
    assert provider._append_terms(rule, "medical_advice") == " (공통)"
    assert provider._append_terms({}, "legal_advice") == ""


def test_serpapi_rule_keys_are_all_consumed_by_code():
    """설정에만 있고 코드가 읽지 않는 키는 '이렇게 검색된다'는 착각을 만든다."""
    consumed = {"domains", "domains_by_type", "keyword_groups_by_type", "query_suffixes_by_domain",
                "max_domains_per_query", "tbs", "append_terms", "append_terms_by_type",
                "query_strategy"}   # source_router.serpapi_rule이 읽는다
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    unused = {k for rule in p2["serpapi"]["rules_by_lv2"].values() for k in rule} - consumed
    assert not unused, f"코드가 읽지 않는 serpapi rule 키: {sorted(unused)}"


def test_every_lv2_runs_two_queries_per_type():
    """max_searches가 type 수와 같으면 ①피해·상담 각도만 돌고 ②사건·보도는 한 번도 안 나간다.

    단일 type LV2는 LV2 공통 쿼리가 type 쿼리를 통째로 밀어냈던 이력이 있어
    공통 쿼리를 두지 않는다(구분이 무의미하고 라운드로빈보다 먼저 나간다).
    """
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    for lv2, intent_cfg in cfg["collection_intents_by_lv2"].items():
        n_types = len(intent_cfg.get("queries_by_type") or {})
        expected = 3 if n_types <= 1 else n_types * 2
        assert intent_cfg["max_searches"] == expected, f"{lv2}: max_searches={intent_cfg['max_searches']}"
        if n_types <= 1:
            assert not intent_cfg.get("queries"), f"{lv2}: 단일 type인데 LV2 공통 쿼리가 있다"


def test_sensitive_lv2_queries_all_scope_to_korea():
    """민감 LV2는 검색어 자체가 국내로 한정돼야 한다. 해외 사건 유입 비용이 특히 크다."""
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    for lv2 in cfg["sensitive_overlay"]:
        for queries in cfg["collection_intents_by_lv2"][lv2]["queries_by_type"].values():
            off = [q for q in queries if not any(k in q for k in ("한국", "국내", "대한민국"))]
            assert not off, f"{lv2}: 국내 맥락이 없는 검색어 {off}"


def test_preview_query_plans_makes_plans_without_searching(tmp_path, monkeypatch):
    """UI 0단계: 검색어만 만들고 검색·fetch·저장은 하지 않는다."""
    called = {"discover": 0, "extract": 0}
    monkeypatch.setattr(QueryPlanner, "plan",
                        lambda self, lv2, d, strat, seeds, low=None, query_kind="", max_queries=None: [_plan("web")])
    monkeypatch.setattr(_gf._router, "discover",
                        lambda *a, **kw: called.__setitem__("discover", called["discover"] + 1) or [])
    monkeypatch.setattr(ExtractorRouter, "extract",
                        lambda *a, **kw: called.__setitem__("extract", called["extract"] + 1))
    plans = pipeline.preview_query_plans(P2, str(tmp_path / "p.db"), ["6_O_CBRNE"], taxonomy_config=TAXO)
    assert list(plans) == ["6_O_CBRNE"] and plans["6_O_CBRNE"]
    assert called == {"discover": 0, "extract": 0}


def test_plan_cache_runs_only_the_approved_queries(tmp_path, monkeypatch):
    """0단계에서 체크 해제한 검색어는 실행되지 않아야 한다."""
    seen = []

    def fake_discover(plans, source, strat, lv2, ctx):
        seen.extend(p.query for p in plans)
        return []

    def never_plan(self, *a, **kw):
        raise AssertionError("승인된 계획이 있는데 planner를 다시 호출했다")

    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    p2["source_strategies_by_lv2"] = {"6_O_CBRNE": _cbrne_strategy(modes=["search_planned"])}
    monkeypatch.setattr(_gf, "load_phase2_config", lambda path: p2)
    monkeypatch.setattr(QueryPlanner, "plan", never_plan)
    monkeypatch.setattr(_gf._router, "discover", fake_discover)
    approved = [_plan("web", query="승인된 검색어")]
    pipeline.small_run(["6_O_CBRNE"], limit=4, config_path=P2, db_path=str(tmp_path / "a.db"),
                       taxonomy_config=TAXO, provider=MockTavilyProvider(),
                       plan_cache={"6_O_CBRNE": approved})
    assert seen == ["승인된 검색어"]


def test_yaml_query_mode_disables_openai_planner(tmp_path, monkeypatch):
    """Streamlit 선택값이 실행 엔진까지 내려가 OpenAI 검색어 생성을 끈다."""
    seen = {}

    def fake_small_run(lv2s, limit, *a, **kw):
        seen.update(kw.get("overrides") or {})
        return {"stored_records": 0, "run_id": "r"}

    monkeypatch.setattr(_gf, "small_run", fake_small_run)
    _gf.run_taxonomy_plan(["6_O_CBRNE"], P2, str(tmp_path / "plan.db"), taxonomy_config=TAXO,
                          query_plan_mode="yaml_random")

    assert seen["query_planner"]["enabled"] is False
    assert seen["query_planner"]["shuffle_fallback_plans"] is True


def test_disabled_query_planner_uses_config_fallback_queries(monkeypatch):
    """OpenAI를 끄면 targeted_collection.yaml의 queries_by_type을 QueryPlan으로 쓴다."""
    from datetime import date

    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    p2["query_planner"] = {**p2["query_planner"], "enabled": False, "max_queries_per_lv2": 2}
    strategy = _gf._merged_strategy("6_O_CBRNE", p2)

    class _Store:
        def load_query_plans(self, lv2):
            return []

        def save_query_plans(self, rows):
            self.rows = rows

    monkeypatch.setattr(_gf, "_seed_signals", lambda *a, **kw: [])
    monkeypatch.setattr(_gf._qp, "should_regenerate", lambda *a, **kw: True)
    plans = _gf._plans_for(_Store(), "6_O_CBRNE", strategy, p2,
                           QueryPlanner(None, p2["query_planner"]), "정의", date(2026, 8, 20))

    assert plans
    assert {p.generation_source for p in plans} == {"config_fallback"}


def test_taxonomy_plan_reports_progress_for_every_step(tmp_path, monkeypatch):
    """실행 단위마다 진행률이 오지 않으면 UI가 멈춘 것처럼 보인다."""
    seen = []

    def fake_small_run(lv2s, limit, *a, **kw):
        cb = kw.get("on_progress")
        assert cb is not None, "run_taxonomy_plan이 on_progress를 넘기지 않았다"
        cb(1, 2)
        return {"stored_records": 0, "run_id": "r"}

    monkeypatch.setattr(_gf, "small_run", fake_small_run)
    _gf.run_taxonomy_plan(["6_O_CBRNE"], P2, str(tmp_path / "p.db"), taxonomy_config=TAXO,
                          on_progress=lambda *args: seen.append(args))
    steps = {(s, total, label) for s, total, label, _, _ in seen}
    assert steps and all(1 <= s <= total for s, total, _ in steps)
    assert any(done for *_, done, _ in seen), "small_run 내부 진행이 전달되지 않았다"


# ── 6_O 재설계: 공식기관 metadata_only seed → 사건 검색어 → 뉴스 본문 저장 ──
def _official_result(url, title):
    return SearchResult(provider="serpapi", target_taxonomy_lv2="6_O_CBRNE",
                        query_or_intent="site:nfa.go.kr 유해화학물질 누출", title=title, url=url,
                        snippet=title, content_hint=title, query_type="chemical",
                        provider_score=0.9, rank=1)


def _seed_strategy():
    return {
        "modes": ["official_seed", "search_expand"], "recency_days": 730,
        "require_event_and_material": True,
        "target_types": {"chemical": {"query_budget": 3}},
        "sources": [
            {"id": "fire_agency", "access": "metadata_only", "method": "official_board",
             "domain": "nfa.go.kr", "priority": 1},
            {"id": "news", "access": "direct", "method": "serpapi_site",
             "domains": ["yna.co.kr", "newsis.com"], "priority": 2},
        ],
        "planner": {"query_kinds": ["event"], "forbidden_intents": ["manufacturing"]},
        "include_by_type": {"chemical": ["유해화학물질", "누출"]},
        "event_terms": ["누출", "사고", "조사", "수사", "검거"],
    }


def test_official_source_is_seed_only_and_news_body_is_stored(tmp_path, monkeypatch):
    """공식기관은 fetch하지 않고 사건 seed로만 쓰고, 최종 콘텐츠는 뉴스 본문이다."""
    seeds_seen, fetched = [], []

    def fake_plan(self, lv2, definition, strat, seeds, low=None, query_kind="", max_queries=None):
        seeds_seen.append([s["title"] for s in seeds])
        return [_plan("fire_agency"), _plan("news", query="화성시 유해화학물질 누출 사고 조사")]

    def fake_discover(plans, source, strat, lv2, ctx):
        if source["id"] == "fire_agency":
            return [_official_result("https://www.nfa.go.kr/bbs/1", "화성 유해화학물질 누출 사고 대응"),
                    _official_result("https://www.nfa.go.kr/bbs/2", "목 차")]   # 목록 페이지 = seed 아님
        return [_result("https://www.yna.co.kr/view/1", "news")]

    def counting_extract(self, c, collected_at, task=None):
        fetched.append(c.source_url)
        return _stub_cbrne_extract(self, c, collected_at, task)

    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    p2["source_strategies_by_lv2"] = {"6_O_CBRNE": _seed_strategy()}
    monkeypatch.setattr(_gf, "load_phase2_config", lambda path: p2)
    monkeypatch.setattr(QueryPlanner, "plan", fake_plan)
    monkeypatch.setattr(_gf._router, "discover", fake_discover)
    monkeypatch.setattr(ExtractorRouter, "extract", counting_extract)
    db = tmp_path / "seed.db"
    pipeline.small_run(["6_O_CBRNE"], limit=6, config_path=P2, db_path=str(db),
                       taxonomy_config=TAXO, provider=MockTavilyProvider())

    # 1) 공식기관은 한 건도 fetch하지 않는다
    assert all("nfa.go.kr" not in url for url in fetched), f"공식기관을 fetch했다: {fetched}"
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM content_records WHERE domain LIKE '%nfa.go.kr%'").fetchone()[0] == 0
    # 2) 최종 저장은 뉴스 본문
    rows = conn.execute("SELECT domain, classification_source FROM content_records").fetchall()
    assert rows and all(d.endswith("yna.co.kr") and src == "targeted_acceptance_gate"
                        for d, src in rows)
    # 3) 공식 후보는 discovery_only로 기록되고 snippet은 후보에만 남는다
    status = dict(conn.execute(
        "SELECT source_id, status FROM url_candidates WHERE domain LIKE '%nfa.go.kr%'").fetchall())
    conn.close()
    assert status == {"fire_agency": "discovery_only"}
    # 4) 사건성 있는 제목만 2라운드 검색어 seed로 넘어간다("목 차"는 제외)
    assert any("화성 유해화학물질 누출 사고 대응" in titles for titles in seeds_seen)
    assert all("목 차" not in titles for titles in seeds_seen)


def test_permissive_collection_fetches_without_prefetch_topic_gate(tmp_path, monkeypatch):
    """2차 대량 수집에서는 사건어·물질어 사전 gate로 본문 후보를 버리지 않는다."""
    fetched = []
    articles = {
        "https://www.yna.co.kr/view/ok": "화성 유해화학물질 누출 사고 조사 착수",   # 물질+사건 ✅
        "https://www.yna.co.kr/view/rule": "유해화학물질 관리 기준 개정 고시",      # 물질만 ❌
        "https://www.yna.co.kr/view/etc": "공장 화재 사고 조사 착수",              # 사건만 ❌
    }

    def fake_discover(plans, source, strat, lv2, ctx):
        if source["id"] != "news":
            return []
        return [_result(url, "news") for url in articles] and [
            SearchResult(provider="serpapi", target_taxonomy_lv2="6_O_CBRNE", query_or_intent="q",
                         title=title, url=url, snippet=title, content_hint=title,
                         query_type="chemical", provider_score=0.9, rank=i)
            for i, (url, title) in enumerate(articles.items(), start=1)]

    monkeypatch.setattr(QueryPlanner, "plan",
                        lambda self, *a, **kw: [_plan("news", query="화성 유해화학물질 누출")])
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    p2["source_strategies_by_lv2"] = {"6_O_CBRNE": {**_seed_strategy(), "modes": ["search_planned"]}}
    monkeypatch.setattr(_gf, "load_phase2_config", lambda path: p2)
    monkeypatch.setattr(_gf._router, "discover", fake_discover)
    monkeypatch.setattr(ExtractorRouter, "extract",
                        lambda self, c, at, task=None: fetched.append(c.source_url)
                        or _stub_cbrne_extract(self, c, at, task))
    db = tmp_path / "gate.db"
    pipeline.small_run(["6_O_CBRNE"], limit=6, config_path=P2, db_path=str(db),
                       taxonomy_config=TAXO, provider=MockTavilyProvider())
    assert fetched == list(articles), fetched
    conn = sqlite3.connect(db)
    skipped = conn.execute(
        "SELECT COUNT(*) FROM url_candidates WHERE status='prefetch_skipped'"
    ).fetchone()[0]
    conn.close()
    assert skipped == 0


def test_explicit_gate_overrides_rerank_skip(tmp_path, monkeypatch):
    """명시 규칙을 통과한 후보를 rerank 점수가 다시 죽이면 안 된다(6_O 0건의 주범)."""
    fetched = []
    title = "화성 유해화학물질 누출 사고 조사"

    from src.phase2.reranker import RerankResult
    monkeypatch.setattr(_gf, "rerank",
                        lambda *a, **kw: RerankResult(0.0, 0.0, "skip", "rule", "강제 skip"))
    monkeypatch.setattr(QueryPlanner, "plan", lambda self, *a, **kw: [_plan("news")])
    monkeypatch.setattr(_gf._router, "discover", lambda plans, source, *a: [
        SearchResult(provider="serpapi", target_taxonomy_lv2="6_O_CBRNE", query_or_intent="q",
                     title=title, url="https://www.yna.co.kr/view/9", snippet=title,
                     content_hint=title, query_type="chemical", provider_score=0.9, rank=1)
    ] if source["id"] == "news" else [])
    monkeypatch.setattr(ExtractorRouter, "extract",
                        lambda self, c, at, task=None: fetched.append(c.source_url)
                        or _stub_cbrne_extract(self, c, at, task))
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    p2["source_strategies_by_lv2"] = {"6_O_CBRNE": {**_seed_strategy(), "modes": ["search_planned"]}}
    monkeypatch.setattr(_gf, "load_phase2_config", lambda path: p2)
    pipeline.small_run(["6_O_CBRNE"], limit=4, config_path=P2, db_path=str(tmp_path / "o.db"),
                       taxonomy_config=TAXO, provider=MockTavilyProvider())
    assert fetched == ["https://www.yna.co.kr/view/9"], "rerank가 명시 규칙 통과분을 죽였다"


def test_law_firm_marketing_is_filtered_but_real_cases_survive():
    """'피해·상담' 검색어는 로펌 마케팅 글을 대량으로 부른다.

    실제 Tavily 응답에서 5건 중 2건이 법무법인 성공사례·FAQ였고 최고점으로 통과했다(2026-08-19).
    감점 어휘는 단일 토큰이어야 한다 — '법무법인 성공사례'는 '법무법인 해든 성공사례'에 안 걸린다.
    """
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    policy = {p.taxonomy_lv2: p for p in load_policies(TAXO)}["1_A_Toxic_Language"]
    intent = build_collection_intent(policy, cfg)
    query = "단톡방 따돌림 저격 논란 반응"
    intent.queries, intent.query_types = [query], {query: "cyberbullying_and_harassment"}

    def decide(title, content):
        result = SearchResult("tavily", "1_A_Toxic_Language", query, title, "https://x/1",
                              snippet=content, content_hint=content,
                              query_type="cyberbullying_and_harassment", provider_score=0.6, rank=1)
        return rerank(result, intent, None, cfg["rerank"]).fetch_decision

    assert decide("단톡방 따돌림 저격글 피해자 조력 성공사례",
                  "법무법인 해든 성공사례. SNS 저격글 욕설 정리. 무료상담 상담예약") == "skip"
    assert decide("단톡방에서 저격당해서 개빡치는데 어떡함",
                  "애들이 나 저격하고 조리돌림하는데 악플까지 달림") == "fetch"


def test_marketing_terms_are_not_applied_to_prohibited_advisory():
    """3_H는 법률·의료 조언 콘텐츠 자체가 목표라 로톡·닥터나우를 감점하면 안 된다."""
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    exclude = cfg["collection_intents_by_lv2"]["3_H_Prohibited_Advisory"]["exclude"]
    assert not ({"법무법인", "성공사례", "무료상담"} & set(exclude))


def test_scoring_vocabulary_covers_the_words_used_in_queries():
    """검색어에 쓰는 어휘가 점수 어휘에 없으면 검색은 맞게 하고 결과는 전부 버린다.

    실측: '단톡방 따돌림 저격' 검색 결과에서 KBS·SBS·연합 실제 보도가 전부 버려졌다.
    include_by_type에 '따돌림·단톡방'이 없었기 때문이다(2026-08-19).
    """
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    intent_cfg = cfg["collection_intents_by_lv2"]["1_A_Toxic_Language"]
    scoring = {t for terms in intent_cfg["include_by_type"].values() for t in terms}
    scoring |= set(intent_cfg.get("include") or [])
    for core in ("따돌림", "단톡방", "학교폭력", "왕따"):
        assert core in scoring, f"검색어에 쓰는 '{core}'가 점수 어휘에 없다"


def test_search_depth_matches_whether_snippets_are_actually_used():
    """snippet으로 fetch를 거르는 LV2가 있으면 advanced가, 없으면 basic이 맞다.

    실측(2026-08-19): basic 12.0 URL/credit(snippet 103자) vs advanced 8.2 URL/credit(1,533자).
    사전 필터를 안 쓰는데 advanced를 사면 크레딧당 URL을 1/3 손해 본다.
    """
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    default = p2["providers"]["tavily"]["search_depth"]
    for lv2, st in p2["source_strategies_by_lv2"].items():
        uses_snippet = (not st.get("permissive_collection", False)
                        and bool(st.get("prefetch_required_groups")
                                 or st.get("require_event_and_material")))
        depth = st.get("search_depth", default)
        if uses_snippet:
            assert depth == "advanced", f"{lv2}: snippet으로 거르는데 {depth}다"
    assert default == "basic", "기본값이 advanced면 snippet을 안 쓰는 LV2까지 비싸게 산다"


# ── 소스별 기준 분리: 한 LV2 안에 성격이 다른 콘텐츠가 있을 때 ──
def _source_of(lv2, source_id):
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    return next(s for s in p2["source_strategies_by_lv2"][lv2]["sources"] if s["id"] == source_id)


def test_source_overrides_relax_only_that_source():
    """1_A는 뉴스 기준이 엄격해야 하지만 디시 원문은 그 조건을 하나도 못 채운다.

    기준을 통째로 풀면 뉴스 쪽에 잡문이 들어오므로 소스 단위로만 완화한다.
    """
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    strategy = _gf._merged_strategy("1_A_Toxic_Language", p2)
    news = _gf._source_strategy(strategy, _source_of("1_A_Toxic_Language", "web_news"))
    dc = _gf._source_strategy(strategy, _source_of("1_A_Toxic_Language", "dcinside"))

    # 뉴스는 본문 길이·한국 근거 방식이 그대로
    assert news["acceptance"]["min_body_chars"] == 200
    assert "korean_organization" in news["acceptance"]["korea_evidence"]
    # 디시만 완화되고, 원본 전략은 오염되지 않는다
    assert dc["acceptance"]["min_body_chars"] == 80
    assert dc["acceptance"]["korea_evidence"] == ["korean_platform_context"]
    assert dc["lv2_risk_signals"] == ["toxic_language"]
    assert strategy["acceptance"]["min_body_chars"] == 200
    # 날짜는 두 소스 모두 필수여야 한다
    assert news["acceptance"]["require_published_at"] is True
    assert dc["acceptance"]["require_published_at"] is True


def test_dcinside_raw_expression_is_accepted_under_its_own_source():
    """1_A는 뉴스 보도와 디시 한국 플랫폼 원문을 함께 수집한다."""
    from datetime import date
    from src.common.site_registry import SiteRegistry
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    strategy = _gf._merged_strategy("1_A_Toxic_Language", p2)
    registry = SiteRegistry.load("configs/site_policy.yaml")
    rec = ContentRecord(
        source_url="https://gall.dcinside.com/board/view/?id=dcbest&no=1",
        domain="gall.dcinside.com", site_name="dcinside", site_type="community",
        taxonomy_lv2_candidate="1_A_Toxic_Language", subtype_candidate="profanity_and_insults",
        title="숲음갤 병신인것도 맞는데", body_text="이 새끼들 진짜 병신같다 씨발 " * 6,
        collected_at="2026-08-19", search_query="board", search_api="board_list",
        extractor="dcinside")
    cand = UrlCandidate(rec.source_url, rec.domain, "board", "board_list",
                        "1_A_Toxic_Language", "profanity_and_insults")
    today = date(2026, 8, 19)
    dc = _gf._source_strategy(strategy, _source_of("1_A_Toxic_Language", "dcinside"))
    news = _gf._source_strategy(strategy, _source_of("1_A_Toxic_Language", "web_news"))
    rec.published_at = "2026-08-15"       # 날짜는 두 기준 모두 필수다
    assert _acceptance_eval(rec, cand, dc, p2, today, registry).accepted
    assert not _acceptance_eval(rec, cand, news, p2, today, registry).accepted


def _acceptance_eval(rec, cand, strategy, p2, today, registry):
    from src.phase2 import acceptance
    return acceptance.evaluate(rec, cand, strategy, p2["default_acceptance"], today, registry)


def test_budget_and_duplicate_are_reported_separately(tmp_path, monkeypatch):
    """한 사유로 뭉치면 원인을 못 가린다.

    실측(2026-08-19 1_A): 136건이 'seen_or_budget_cap'이었는데 진짜 중복은 3건뿐이고
    133건이 fetch 상한에 막힌 것이었다. 179건 중 4건만 저장된 이유가 여기 있었다.
    """
    results = [_result(f"https://www.yna.co.kr/view/{i}", "web") for i in range(6)]
    strategy = _cbrne_strategy(modes=["search_planned"],
                              max_fetch_per_lv2=2, sources=[
                                  {"id": "web", "access": "direct", "method": "web_search", "priority": 1}])
    db, _, _ = _run_strategy(tmp_path, monkeypatch, strategy=strategy,
                             results_by_source={"web": results})
    conn = sqlite3.connect(db)
    by_status = dict(conn.execute(
        "SELECT status, COUNT(*) FROM url_candidates GROUP BY 1").fetchall())
    reasons = {r[0] for r in conn.execute(
        "SELECT filter_reason FROM url_candidates WHERE status='budget_exceeded'")}
    conn.close()
    assert by_status.get("budget_exceeded"), f"예산 초과가 별도 상태로 기록되지 않았다: {by_status}"
    assert reasons == {"max_fetch_per_lv2=2"}, reasons


def test_permissive_collection_still_drops_stale_content(tmp_path, monkeypatch):
    """recency는 품질 기준이 아니라 수집 범위 정의다. 다 모으더라도 창 밖은 안 모은다.

    실측(2026-08-19 1_A): Tavily start_date를 365일로 걸었는데 저장 97건 중 65건이
    1,000일을 넘겼다. topic=general에서 provider 날짜 필터는 신뢰할 수 없다.
    """
    def _stale_extract(self, c, collected_at, task=None):
        outcome = _stub_cbrne_extract(self, c, collected_at, task)
        outcome.record.published_at = "2019-01-01" if "old" in c.source_url else "2026-08-10"
        return outcome

    strategy = _cbrne_strategy(modes=["search_planned"], permissive_collection=True,
                               recency_days=365, sources=[
                                   {"id": "web", "access": "direct", "method": "web_search",
                                    "priority": 1}])
    db, _, _ = _run_strategy(
        tmp_path, monkeypatch, strategy=strategy, extract=_stale_extract,
        results_by_source={"web": [_result("https://www.yna.co.kr/view/old1", "web"),
                                   _result("https://www.yna.co.kr/view/fresh1", "web")]})
    conn = sqlite3.connect(db)
    stored = [r[0] for r in conn.execute("SELECT source_url FROM content_records")]
    stale = conn.execute(
        "SELECT COUNT(*) FROM url_candidates WHERE filter_reason LIKE 'stale%'").fetchone()[0]
    conn.close()
    assert stored == ["https://www.yna.co.kr/view/fresh1"], stored
    assert stale == 1, "창 밖 콘텐츠가 stale로 기록되지 않았다"


def test_permissive_collection_still_drops_non_korean_content(tmp_path, monkeypatch):
    """모든 taxonomy에서 비한국어 본문은 permissive_collection으로도 저장하지 않는다."""
    english = ("South Korea officials investigated a chemical leak incident and police reported "
               "victim damage after the accident. ") * 8
    strategy = _cbrne_strategy(modes=["search_planned"], permissive_collection=True,
                               sources=[{"id": "web", "access": "direct", "method": "web_search",
                                         "priority": 1}])
    db, _, _ = _run_strategy(
        tmp_path, monkeypatch, strategy=strategy, extract=_body_extract(
            english, "South Korea chemical leak investigation"),
        results_by_source={"web": [_result("https://www.reuters.com/world/korea/1", "web")]})
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT COUNT(*) FROM content_records").fetchone()[0]
    reason = conn.execute(
        "SELECT filter_reason FROM url_candidates WHERE status='quality_failed'").fetchone()[0]
    conn.close()
    assert stored == 0
    assert reason.startswith("not_korean")


def test_ui_recency_override_reaches_both_search_and_storage(tmp_path, monkeypatch):
    """수집 기간은 검색 요청과 저장 판정 양쪽에 걸려야 한다.

    한쪽만 걸리면 이미 크레딧을 쓴 결과를 저장 단계에서 버리거나(낭비),
    창 밖 콘텐츠를 그대로 저장한다(오염).
    """
    def _old_extract(self, c, collected_at, task=None):
        outcome = _stub_cbrne_extract(self, c, collected_at, task)
        outcome.record.published_at = "2025-01-01"   # 90일 창 밖, 730일 창 안
        return outcome

    strategy = _cbrne_strategy(modes=["search_planned"], permissive_collection=True,
                               recency_days=730, sources=[
                                   {"id": "web", "access": "direct", "method": "web_search",
                                    "priority": 1}])
    db, _, calls = _run_strategy(
        tmp_path, monkeypatch, strategy=strategy, extract=_old_extract,
        results_by_source={"web": [_result("https://www.yna.co.kr/view/1", "web")]},
        overrides={"strategy_by_lv2": {"6_O_CBRNE": {"recency_days": 90}}})
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT COUNT(*) FROM content_records").fetchone()[0]
    conn.close()
    assert calls["recency"] == [90], f"검색 요청에 UI 값이 안 실렸다: {calls['recency']}"
    assert stored == 0, "저장 판정에 UI 값이 안 실려 창 밖 콘텐츠가 들어왔다"


def test_already_collected_lv2_still_searches(tmp_path, monkeypatch):
    """이미 많이 모아둔 LV2도 검색을 건너뛰지 않는다.

    예전에는 '목표 - 이미 모은 수'로 살 검색어를 정해서, 목표를 넘긴 LV2는 검색어 0개 →
    후보 0건 → 저장 0건으로 조용히 끝났다. 화면에는 "완료"만 떠서 오류처럼 보였다.
    """
    strategy = _cbrne_strategy(modes=["search_planned"], sources=[
        {"id": "web", "access": "direct", "method": "web_search", "priority": 1}])
    _, _, calls = _run_strategy(
        tmp_path, monkeypatch, strategy=strategy,
        results_by_source={"web": [_result("https://www.yna.co.kr/view/1", "web")]})
    bought = calls["max_queries"]
    assert bought and bought[0] > 0, "검색어를 한 개도 사지 않았다"


def test_yaml_query_preview_matches_what_will_run():
    """UI가 보여준 검색어와 실제로 나가는 검색어가 같아야 한다.

    seed 없이 shuffle하면 미리보기와 실행이 어긋나 '보여준 것과 다른 게 나갔다'가 된다.
    """
    ov = lambda seed: {"query_planner": {"enabled": False, "shuffle_fallback_plans": True,
                                         "shuffle_seed": seed, "max_queries_per_lv2": 5}}
    first = _gf.preview_yaml_queries(P2, ["1_A_Toxic_Language"], ov(7))
    again = _gf.preview_yaml_queries(P2, ["1_A_Toxic_Language"], ov(7))
    assert first == again, "같은 seed인데 뽑기 결과가 달라졌다"
    other = _gf.preview_yaml_queries(P2, ["1_A_Toxic_Language"], ov(99))
    assert first != other, "seed를 바꿔도 같은 검색어만 나온다 = '다시 뽑기'가 무의미"
    picked = [q["query"] for q in first["1_A_Toxic_Language"]["queries"]]
    assert len(picked) == 5 and len(set(picked)) == 5


def test_yaml_query_preview_shows_provider_and_idle_sources():
    """어느 검색 API로 몇 건 나가는지, 안 나가는 수집원은 무엇인지 화면이 알 수 있어야 한다.

    1_A는 primary_then_secondary라 검색어 예산이 YAML 검색어 수보다 작으면 Tavily만 나간다.
    그 사실이 안 보이면 "SerpAPI 뉴스 검색도 도는 줄" 알게 된다.
    """
    ov = {"query_planner": {"enabled": False, "shuffle_fallback_plans": True,
                            "shuffle_seed": 0, "max_queries_per_lv2": 10}}
    got = _gf.preview_yaml_queries(P2, ["1_A_Toxic_Language"], ov)["1_A_Toxic_Language"]
    assert {q["provider"] for q in got["queries"]} == {"Tavily", "SerpAPI"}
    # 검색어를 쓰지 않는 게시판 수집원도 함께 알려준다.
    assert [b["source_id"] for b in got["boards"]] == ["dcinside"]
    # 설정된 수집원이 전부 이번 실행에 쓰인다.
    assert got["idle"] == []


def test_naver_kin_tag_pages_are_excluded_in_the_query():
    """지식인을 검색하는 LV2는 태그·목록 페이지를 검색식에서 뺀다.

    여러 도메인을 site: OR 한 줄로 묶어 보낼 때는 domain이 None이라 도메인별 suffix가
    잡히지 않았다 — 그래서 -inurl:tag를 설정해 둔 LV2들도 실제로는 적용되지 않았다.
    """
    from src.phase2 import source_router as sr
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    for lv2 in ("1_B_Sexual_Content", "1_D_Child_Exploitation", "3_H_Prohibited_Advisory",
                "1_C_Self_Harm", "2_E_Discrimination", "5_L_Illegal_Activity",
                "5_N_Encouraging_Unethical_Actions"):
        strategy = _gf._merged_strategy(lv2, p2)
        source = next(s for s in strategy["sources"]
                      if "kin.naver.com/qna" in (s.get("domains") or []))
        # 검색 대상은 /qna 경로로 좁혀 둔다(사이트 전체를 훑으면 태그 페이지가 딸려 온다).
        assert "kin.naver.com" not in (source.get("domains") or []), f"{lv2}: /qna로 좁히지 않았다"
        query = sr.final_query(_plan(source["id"], query="피해 상담"), source, p2, lv2)
        assert "-inurl:tag" in query, f"{lv2}: 태그 제외가 검색식에 안 붙었다 — {query}"
        rule = sr.serpapi_rule(source, strategy, p2, lv2)
        suffixes = rule["query_suffixes_by_domain"]
        # 묶음 검색(site: OR)이면 "*"로, 도메인별 호출이면 해당 도메인 키로 실려야 한다.
        assert suffixes.get("*") or suffixes.get("kin.naver.com/qna"), \
            f"{lv2}: 태그 제외가 rule에 안 실렸다"


def test_serpapi_rules_reach_the_strategy_path():
    """serpapi.rules_by_lv2에 적은 값은 전략 경로에서도 실제로 쓰여야 한다.

    예전에는 source_router가 자체 rule을 만들어 domains_by_type·keyword_groups_by_type·
    max_domains_per_query·tbs를 통째로 무시했다 — '설정했는데 안 먹는' 상태였다.
    """
    from src.phase2 import source_router as sr
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    lv2 = "1_B_Sexual_Content"
    configured = p2["serpapi"]["rules_by_lv2"][lv2]
    strategy = _gf._merged_strategy(lv2, p2)
    source = next(s for s in strategy["sources"] if s.get("id") == "topic_sites")
    rule = sr.serpapi_rule(source, strategy, p2, lv2)
    assert rule["domains_by_type"] == configured["domains_by_type"]
    assert rule["keyword_groups_by_type"] == configured["keyword_groups_by_type"]
    assert rule["max_domains_per_query"] == configured["max_domains_per_query"] == 3
    assert rule["tbs"] == configured["tbs"]


def test_site_per_query_costs_one_credit_per_domain():
    """site_per_query는 도메인마다 따로 호출한다 = 크레딧이 도메인 수만큼 는다."""
    from src.phase2 import source_router as sr
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    strategy = _gf._merged_strategy("1_B_Sexual_Content", p2)
    source = next(s for s in strategy["sources"] if s.get("id") == "topic_sites")
    rule = sr.serpapi_rule(source, strategy, p2, "1_B_Sexual_Content")
    assert "append_terms" not in rule, "site_per_query인데 site: OR 묶음으로 나간다"

    policy = next(p for p in load_policies(TAXO) if p.taxonomy_lv2 == "1_B_Sexual_Content")
    intent = build_collection_intent(policy, p2)
    provider = SerpApiProvider(p2["serpapi"]["provider"], {"1_B_Sexual_Content": rule})
    terms = provider._search_terms(intent)
    calls = sum(len(provider._domains(intent, q, t) or [None]) for q, t in terms)
    assert calls == len(terms) * 3 - 2, (calls, len(terms))   # 한 type만 도메인 2개


def test_preview_shows_the_request_that_actually_goes_out():
    """미리보기의 검색식·옵션은 실행이 쓰는 함수에서 그대로 나와야 한다.

    화면용으로 따로 계산하면 설정을 바꿨을 때 둘이 조용히 어긋난다.
    """
    from src.phase2 import source_router as sr
    ov = {"query_planner": {"enabled": False, "shuffle_fallback_plans": True,
                            "shuffle_seed": 0, "max_queries_per_lv2": 10}}
    got = _gf.preview_yaml_queries(P2, ["1_A_Toxic_Language"], ov)["1_A_Toxic_Language"]

    # SerpAPI 다중 도메인은 site: OR 한 줄로 묶여 1회 호출이 된다.
    serp = next(q for q in got["queries"] if q["provider"] == "SerpAPI")
    assert serp["final_query"].startswith(serp["query"])
    assert serp["final_query"].count("site:") == 5 and " OR " in serp["final_query"]
    # Tavily는 검색어를 그대로 보낸다.
    tav = next(q for q in got["queries"] if q["provider"] == "Tavily")
    assert tav["final_query"] == tav["query"]

    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    strategy = _gf._merged_strategy("1_A_Toxic_Language", p2)
    by_id = {s["id"]: s for s in strategy["sources"]}
    shown = {e["source_id"]: e["options"] for e in got["execution"]}

    live_tavily = sr.tavily_options(by_id["web_news"], strategy, p2, date.today())
    assert shown["web_news"]["검색 깊이"] == live_tavily["search_depth"]
    assert shown["web_news"]["색인"] == live_tavily["topic"]
    assert shown["web_news"]["발행일 하한"] == live_tavily["start_date"]

    live_serp = sr.serpapi_rule(by_id["news_sites"], strategy, p2, "1_A_Toxic_Language")
    assert shown["news_sites"]["기간 필터"] == live_serp["tbs"]
    # 게시판은 검색 API를 쓰지 않는다.
    assert "크레딧 0" in shown["dcinside"]["방식"]


def test_source_mix_holds_the_ratio_at_every_budget():
    """채널 비율은 예산과 무관하게 유지돼야 한다.

    예전 primary_then_secondary는 '검색어를 더 살 때만 SerpAPI 확장'이라, 기본 예산에서는
    1_A가 Tavily만 돌고 뉴스 SerpAPI가 한 번도 안 불렸다.
    """
    for budget, expected_tavily in ((10, 7), (20, 10)):
        ov = {"query_planner": {"enabled": False, "shuffle_fallback_plans": True,
                                "shuffle_seed": 0, "max_queries_per_lv2": budget}}
        got = _gf.preview_yaml_queries(P2, ["1_A_Toxic_Language"], ov)["1_A_Toxic_Language"]
        mix = Counter(q["provider"] for q in got["queries"])
        assert mix["SerpAPI"] > 0, f"예산 {budget}에서 SerpAPI가 한 번도 안 나갔다"
        assert mix["Tavily"] == expected_tavily, (budget, mix)


def test_source_mix_interleaves_so_truncation_keeps_the_ratio():
    """비율 배분은 앞에 몰지 않고 번갈아 낸다. 몰면 예산 절단 뒤 비율이 무너진다."""
    strategy = {
        "planner": {"source_mix": {"a": 70, "b": 30}},
        "sources": [{"id": "a", "method": "web_search", "access": "direct", "priority": 1},
                    {"id": "b", "method": "serpapi_site", "access": "direct", "priority": 2}],
    }
    plans = [_plan("a", query=f"검색어 {i}") for i in range(10)]
    mixed = qp.balance_sources(plans, strategy)
    assert Counter(p.source_id for p in mixed) == {"a": 7, "b": 3}
    # 앞 5개만 잘라도 두 채널이 모두 살아 있어야 한다.
    assert len({p.source_id for p in mixed[:5]}) == 2, [p.source_id for p in mixed]


def test_yaml_queries_come_from_config_not_openai_cache(tmp_path, monkeypatch):
    """YAML 모드는 캐시된 OpenAI 계획을 재사용하지 않는다.

    캐시가 신선하면 _plans_for가 캐시를 먼저 돌려줘서, 'YAML 쿼리 사용'을 골라도
    예전 OpenAI 검색어가 그대로 나가던 문제가 있었다.
    """
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    p2["query_planner"] = {**p2["query_planner"], "enabled": False,
                           "shuffle_fallback_plans": True, "shuffle_seed": 1}
    strategy = p2["source_strategies_by_lv2"]["1_A_Toxic_Language"]

    class _Store:
        conn = None
        def load_query_plans(self, lv2):   # 신선한 OpenAI 캐시가 있는 상황
            return [_plan("news", query="캐시된 OpenAI 검색어").to_row("fp", "2026-08-21")]
        def save_query_plans(self, rows):
            pass
    monkeypatch.setattr(_gf, "_seed_signals", lambda *a, **kw: [])

    plans = _gf._plans_for(_Store(), "1_A_Toxic_Language", strategy, p2, None, "정의",
                           date(2026, 8, 21), current_count=0)
    assert plans, "YAML 검색어가 하나도 안 나왔다"
    assert all(p.generation_source == "config_fallback" for p in plans), \
        f"캐시된 OpenAI 계획이 나갔다: {[p.generation_source for p in plans]}"
    assert "캐시된 OpenAI 검색어" not in [p.query for p in plans]


def test_yaml_query_mode_balances_sources_for_sexual_content():
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    strategy = _gf._merged_strategy("1_B_Sexual_Content", p2)
    plans = _gf.yaml_query_plans("1_B_Sexual_Content", strategy, p2, max_total=12)
    assert {"web", "news_sites", "topic_sites"} <= {p.source_id for p in plans}


def test_toxic_yaml_splits_tavily_and_serpapi_by_ratio():
    """1_A는 source_mix(70:30)로 두 채널을 함께 돌린다. 예산이 작아도 마찬가지다."""
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    strategy = _gf._merged_strategy("1_A_Toxic_Language", p2)

    cheap = _gf.yaml_query_plans("1_A_Toxic_Language", strategy, p2, max_total=10)
    assert Counter(p.source_id for p in cheap) == {"web_news": 7, "news_sites": 3}

    # YAML 검색어(15개)를 넘겨 사려 해도 있는 만큼만 나가고 비율은 유지된다.
    expanded = _gf.yaml_query_plans("1_A_Toxic_Language", strategy, p2, max_total=30)
    assert len(expanded) == 15
    assert Counter(p.source_id for p in expanded) == {"web_news": 10, "news_sites": 5}


def test_query_plan_limits_stop_when_reference_db_already_meets_target(tmp_path):
    db = Store(str(tmp_path / "ref.db"))
    for i in range(100):
        db.conn.execute(
            """INSERT INTO content_records
               (content_id, taxonomy_lv2, canonical_url, action, is_supplementary)
               VALUES (?, '1_A_Toxic_Language', ?, 'accepted', 0)""",
            (f"c{i}", f"https://example.com/{i}"),
        )
    db.conn.commit()
    db.close()

    limits = _gf.query_plan_limits(P2, str(tmp_path / "empty.db"),
                                   ["1_A_Toxic_Language"], reference_db=str(tmp_path / "ref.db"))
    assert limits["1_A_Toxic_Language"] == 0


def test_foreign_language_editions_are_dropped_before_fetch():
    """국내 언론 영어판은 fetch 전에 끊는다.

    같은 도메인이라 site: 검색에 딸려 오는데 본문이 한국어가 아니라 항상 not_korean으로
    버려진다. 뒤에서 버리면 fetch 예산만 먹는다(실측: donga.com/en 6건).
    """
    blocked = [
        "https://www.donga.com/en/article/all/20260511/6221284/1",
        "https://en.yna.co.kr/view/AEN20260101",
        "https://english.hani.co.kr/arti/1234.html",
        "https://www.chosun.com/english/national/2026/01/01/ABC/",
    ]
    for url in blocked:
        assert _gf._non_article_url_reason(url).startswith("foreign_edition"), url
    # 한국어 기사와 'en'이 부분문자열인 경로는 통과해야 한다(오탐 금지).
    for url in [
        "https://sports.donga.com/ent/article/all/20260703/134229289/1",
        "https://www.donga.com/news/article/all/20260511/1",
        "https://www.yna.co.kr/view/AKR20260101",
        "https://m.dcinside.com/board/enter/123",
    ]:
        assert _gf._non_article_url_reason(url) == "", url


def test_run_query_cap_limits_search_credits(tmp_path, monkeypatch):
    """UI의 '카테고리당 검색어 수'가 실제 검색 호출 수를 줄여야 한다.

    크레딧은 검색 호출에서만 나간다(본문 fetch는 HTTP라 무료). 그래서 이 손잡이가
    이번 실행 비용을 정하는 유일한 값이다.
    """
    strategy = _cbrne_strategy(modes=["search_planned"], sources=[
        {"id": "web", "access": "direct", "method": "web_search", "priority": 1}])
    _, _, calls = _run_strategy(
        tmp_path, monkeypatch, strategy=strategy,
        results_by_source={"web": [_result("https://www.yna.co.kr/view/1", "web")]},
        overrides={"query_planner": {"max_queries_per_lv2": 3}})
    assert calls["max_queries"] and max(calls["max_queries"]) == 3, \
        f"검색어 상한 3을 넘겨 샀다: {calls['max_queries']}"


def test_taxonomy_coordinator_forwards_the_run_caps(monkeypatch):
    """UI 슬라이더가 coordinator를 지나 small_run override까지 도달하는지."""
    seen = {}

    def fake_small_run(lv2s, limit, *a, **kw):
        seen.update(kw.get("overrides") or {})
        return {"run_stored": 0, "run_candidates": 0, "skipped": {}, "runs": []}

    monkeypatch.setattr(_gf, "small_run", fake_small_run)
    monkeypatch.setattr(_gf, "preview_taxonomy_plan",
                        lambda *a, **kw: [{"lv2": "6_O_CBRNE", "shortfall": 0, "types": [],
                                           "provider_order": ["tavily"]}])
    _gf.run_taxonomy_plan(["6_O_CBRNE"], P2, "x.db",
                          max_queries_per_lv2=7, max_total_fetch=25)
    assert seen["query_planner"]["max_queries_per_lv2"] == 7
    assert seen["limits"]["max_total_fetch"] == 25


def test_cached_plans_respect_the_query_cap(tmp_path, monkeypatch):
    """계획을 재사용할 때도 max_queries_per_lv2 상한을 넘기지 않는다."""
    from datetime import date
    from src.phase2 import query_planner as _qp

    rows = [_qp.QueryPlan(f"국내 유해화학물질 누출 사고 {i}", "6_O_CBRNE", "chemical",
                          "event", "web", ["환경부"], ["누출"]).to_row("fp", "2026-08-20")
            for i in range(30)]
    monkeypatch.setattr(_gf, "_seed_signals", lambda *a, **kw: [])
    monkeypatch.setattr(_qp, "seed_fingerprint", lambda seeds: "fp")
    monkeypatch.setattr(_qp, "should_regenerate", lambda *a, **kw: False)

    class _Store:
        def load_query_plans(self, lv2):
            return rows

    strategy = _cbrne_strategy(modes=["search_planned"], sources=[
        {"id": "web", "access": "direct", "method": "web_search", "priority": 1}])
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    plans = _gf._plans_for(_Store(), "6_O_CBRNE", strategy, p2, None, "정의",
                           date(2026, 8, 20), current_count=0)
    assert [p.generation_source for p in plans] == ["cached_plan"] * len(plans)
    assert len(plans) == min(len(rows), _qp.max_queries(p2["query_planner"])), \
        f"캐시 계획 {len(plans)}개가 상한을 넘겼다"


def test_ui_approved_plans_are_capped_by_max_queries(monkeypatch):
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    strategy = _cbrne_strategy(modes=["search_planned"], sources=[
        {"id": "web", "access": "direct", "method": "web_search", "priority": 1}])
    approved = [_plan("web", query=f"국내 유해화학물질 누출 사건 {i}") for i in range(30)]
    monkeypatch.setattr(_gf, "_strategy_entries", lambda plans, *a, **kw: plans)

    rounds = _gf._strategy_rounds(
        None, "6_O_CBRNE", strategy, p2, None, "정의", date(2026, 8, 20),
        None, None, {}, [], approved=approved, current_count=90)
    # 이미 90건을 모았어도 잘라내지 않는다 — 상한(max_queries_per_lv2)만 적용된다.
    assert len(rounds[0]()) == min(len(approved), qp.max_queries(p2["query_planner"]))


def test_zero_candidate_run_reports_why(tmp_path, monkeypatch, caplog):
    """후보 0건으로 끝난 실행은 이유를 리포트에 남긴다.

    화면에 "완료"만 뜨고 저장이 0건이면 사용자는 오류로 읽는다. 실제로 그런 신고가 있었다.
    """
    strategy = _cbrne_strategy(modes=["search_planned"], sources=[
        {"id": "web", "access": "direct", "method": "web_search", "priority": 1}])
    with caplog.at_level("WARNING"):
        _, report, _ = _run_strategy(tmp_path, monkeypatch, strategy=strategy,
                                     results_by_source={"web": []})
    assert report["run_stored"] == 0 and report["run_candidates"] == 0
    assert "6_O_CBRNE" in report["skipped"], "후보 0건인데 이유가 안 남았다"
    assert report["skipped"]["6_O_CBRNE"]["detail"]
    assert any("후보 0건" in r.getMessage() for r in caplog.records)


def test_strategy_fetch_ceiling_is_never_below_global_default():
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    default = p2["limits"]["max_fetch_per_lv2"]
    for lv2, strategy in p2["source_strategies_by_lv2"].items():
        assert strategy.get("max_fetch_per_lv2", default) >= default, f"{lv2}: 전역보다 낮은 상한"


def test_no_per_lv2_split_collects_until_the_total_budget(tmp_path, monkeypatch):
    """taxonomy별로 배분하지 않는다. 앞 LV2가 총예산을 다 쓰면 그대로 다 쓴다.

    LV2별 상한은 이미 검색비를 치른 후보를 버리는 장치였다(179건 중 133건).
    폭주 방어는 max_total_fetch 하나로 본다.
    """
    fetched = []
    per_lv2 = {"6_O_CBRNE": [_result(f"https://www.yna.co.kr/view/o{i}", "web") for i in range(30)],
               "4_I_Privacy_Infringement": [_result(f"https://www.yna.co.kr/view/i{i}", "web")
                                            for i in range(30)]}
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    strategy = _cbrne_strategy(modes=["search_planned"], sources=[
        {"id": "web", "access": "direct", "method": "web_search", "priority": 1}])
    p2["source_strategies_by_lv2"] = {lv2: dict(strategy) for lv2 in per_lv2}
    p2["limits"] = {**p2["limits"], "max_fetch_per_lv2": 0, "max_total_fetch": 20,
                    "max_selected_lv2": 2}
    monkeypatch.setattr(_gf, "load_phase2_config", lambda path: p2)
    monkeypatch.setattr(QueryPlanner, "plan", lambda self, lv2, *a, **kw: [_plan("web")])
    monkeypatch.setattr(_gf._router, "discover",
                        lambda plans, source, strat, lv2, ctx: per_lv2.get(lv2, []))
    monkeypatch.setattr(ExtractorRouter, "extract",
                        lambda self, c, at, task=None: fetched.append(c.source_url)
                        or _stub_cbrne_extract(self, c, at, task))
    pipeline.small_run(list(per_lv2), limit=0, config_path=P2,
                       db_path=str(tmp_path / "nosplit.db"), taxonomy_config=TAXO,
                       provider=MockTavilyProvider())
    assert len(fetched) == 20, "총예산까지 모으지 않았다"
    assert len({u for u in fetched}) == 20, "중복 URL을 fetch했다"


def test_budget_exceeded_only_reports_a_real_ceiling():
    """LV2별 상한이 0이면 budget_exceeded 사유가 LV2 상한을 지목하면 안 된다."""
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    assert p2["limits"]["max_fetch_per_lv2"] == 0
    assert all("max_fetch_per_lv2" not in s
               for s in p2["source_strategies_by_lv2"].values())


# 실측(2026-08-19) 쿼리당 고유 결과 수와 저장 수율. 설정을 바꿀 땐 이 값도 재측정할 것.
RESULTS_PER_QUERY = {"basic": 12, "advanced": 17}
DEDUP_KEEP_RATE = 0.87
# permissive_collection 체제의 수율. 단계별 실측(phase2_pilot.db)에서 도출한다.
#   중복 URL 제외 0.79 × 추출 성공 0.80(디시 레이트리밋 114건 제외) ≈ 0.63
# robots 차단·간헐 실패를 감안해 0.45로 보수적으로 잡는다. 실주행 후 재측정할 것.
STORE_YIELD = 0.45


def test_every_strategy_budget_fits_the_query_cap():
    """type별 예산 합이 상한을 넘으면 뒤쪽 type은 통째로 잘려 한 번도 안 나간다."""
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    for lv2, strategy in p2["source_strategies_by_lv2"].items():
        queries = sum(t["query_budget"] for t in strategy["target_types"].values())
        assert queries <= p2["query_planner"]["max_queries_per_lv2"], \
            f"{lv2}: 예산 합 {queries}가 상한에 잘려 낭비된다"


def test_multi_domain_news_source_searches_all_of_them():
    """뉴스 매체를 한 소스로 묶으면 검색식이 OR로 나가야 매체별 호출이 늘지 않는다."""
    from src.phase2.source_router import final_query
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    source = next(s for s in p2["source_strategies_by_lv2"]["1_A_Toxic_Language"]["sources"]
                  if s["id"] == "news_sites")
    query = final_query(_plan("news_sites", query="온라인 모욕 사건 수사"), source, p2,
                        "1_A_Toxic_Language")
    assert " OR " in query
    for domain in source["domains"]:
        assert f"site:{domain}" in query

    # 미리보기가 OR인데 실제로는 매체별로 나가면 크레딧이 매체 수만큼 든다.
    from src.phase2 import source_router
    from src.phase2.provider import SerpApiProvider
    captured = {}

    class _Spy(SerpApiProvider):
        def __init__(self, options, rules):
            super().__init__(options, rules)
            captured.update(rules)

        def search(self, intent):       # conftest가 막은 실검색 대신 검색어만 재현한다
            captured["queries"] = [
                (q, self._domains(intent, q, qt) or [None]) for q, qt in self._search_terms(intent)]
            return []

    ctx = source_router.RouterContext(p2=p2, registry=None, fetcher=None, serpapi_factory=_Spy)
    source_router.discover([_plan("news_sites", query="온라인 모욕 사건 수사")], source,
                           p2["source_strategies_by_lv2"]["1_A_Toxic_Language"],
                           "1_A_Toxic_Language", ctx)
    calls = sum(len(domains) for _, domains in captured["queries"])
    assert calls == 1, f"매체 {len(source['domains'])}개에 {calls}회 검색 = 크레딧 {calls}배"
    for domain in source["domains"]:
        assert f"site:{domain}" in captured["queries"][0][0]


def test_news_topic_is_passed_through_for_toxic_language():
    """1_A web_news는 뉴스 색인만 본다.

    실측(2026-08-20): topic=general이면 후보 33건 중 15건이 로펌·법률상담·논문 SEO였고
    도메인 꼬리가 끝없어 차단 목록으로는 못 따라갔다. 같은 변경이 start_date도 살린다.
    """
    from src.phase2 import source_router
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    strategy = p2["source_strategies_by_lv2"]["1_A_Toxic_Language"]
    source = next(s for s in strategy["sources"] if s["id"] == "web_news")
    assert source.get("tavily_topic") == "news", "web_news가 일반 웹 색인을 본다"

    seen = {}

    class _Spy:
        def __init__(self, options):
            seen.update(options)

        def search(self, intent):
            return []

        def usage_summary(self):
            return {"queries": []}

    ctx = source_router.RouterContext(p2=p2, registry=None, fetcher=None, tavily_factory=_Spy)
    source_router.discover([_plan("web_news")], source, strategy, "1_A_Toxic_Language", ctx)
    assert seen["topic"] == "news"
    assert seen.get("start_date"), "발행일 하한이 실리지 않았다"
    assert set(seen["include_domains"]) == set(p2["providers"]["tavily"]["korean_news_domains"])


def test_toxic_dcinside_is_not_blocked_by_news_source_default():
    from src.phase2 import source_router
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    source = next(s for s in p2["source_strategies_by_lv2"]["1_A_Toxic_Language"]["sources"]
                  if s["id"] == "dcinside")
    ctx = source_router.RouterContext(p2=p2, registry=None, fetcher=None)
    assert p2["default_acceptance"]["require_news_source"] is False
    assert source_router._requires_news_source(ctx, source) is False
