"""2차 semantic targeted discovery 검증 (오프라인, assert 기반).

coverage/intent/provider/reranker 단위 + small_run end-to-end(provider/extract/LLM monkeypatch).
불변식: API content_hint는 본문 저장 금지, 추출 실패 시 미저장, korea 낮으면 excluded,
target/predicted 분리, provenance(run_id/phase=2), opportunistic 판정.
"""
import sqlite3

import pytest
import yaml

from src import pipeline
from src.reporting import coverage
from src.extract import ExtractorRouter
from src.extract.base import ExtractionOutcome
from src.classify.matcher import LLMMatcher
from src.phase2.intent_builder import (
    build_collection_intent,
    missing_manual_intents,
    validate_collection_intent,
)
from src.phase2.provider import MockTavilyProvider, SearchResult, SerpApiProvider, to_candidate
from src.phase2.reranker import rerank
from src.policy import load_policies
from src.schema import ContentRecord, MatchResult, UrlCandidate
from src.storage.store import Store
from src.site_registry import SiteRegistry

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
    assert len(intent.queries) == len(policy.subtypes) == 5
    assert set(intent.query_types.values()) == {st.name for st in policy.subtypes}
    assert all("한국" in query for query in intent.queries)

    rule = cfg["serpapi"]["rules_by_lv2"]["6_O_CBRNE"]
    provider = SerpApiProvider(cfg["serpapi"]["provider"], {"6_O_CBRNE": rule})
    assert rule["query_strategy"] == "site_per_query"
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
    assert all(rule["query_strategy"] in {"site_per_query", "broad_then_site"}
               for rule in rules.values())


def test_prohibited_advisory_serpapi_domains_are_split_by_type():
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))["serpapi"]
    rule = cfg["rules_by_lv2"]["3_H_Prohibited_Advisory"]
    assert set(rule["domains_by_type"]) == {"financial_advice", "legal_advice", "medical_advice"}
    assert "kin.naver.com" in rule["domains_by_type"]["financial_advice"]
    assert "lawtalk.co.kr" in rule["reference_domains_by_type"]["legal_advice"]
    assert "hidoc.co.kr" in rule["reference_domains_by_type"]["medical_advice"]


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


def test_reranker_does_not_treat_serpapi_rank_as_taxonomy_relevance():
    intent = build_collection_intent(
        {p.taxonomy_lv2: p for p in load_policies(TAXO)}["4_I_Privacy_Infringement"],
        {"providers": {"tavily": {}}})
    irrelevant = SearchResult(
        "serpapi", "4_I_Privacy_Infringement", "site:kin.naver.com unrelated", "일반 질문",
        "https://kin.naver.com/qna/1", "일반 질문 본문", provider_score=1.0,
    )
    assert rerank(irrelevant, intent).fetch_decision == "skip"


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
    assert pipeline._phase2_adjudicate(ok, _adj_rec(0.9), "4_I", p2, lambda l: 5)[0] == "accepted"
    assert pipeline._phase2_adjudicate(ok, _adj_rec(0.3), "4_I", p2, lambda l: 5) == ("discard", "low_korea_relevance")
    # 불일치 + predicted 부족 → accepted(opportunistic)
    mm = MatchResult(is_relevant=True, taxonomy_lv2="2_F", subtype="gender", confidence=0.9, reason="", source="llm")
    assert pipeline._phase2_adjudicate(mm, _adj_rec(0.9), "4_I", p2, lambda l: 5)[0] == "accepted"
    # 불일치 + predicted 충분 → discard
    assert pipeline._phase2_adjudicate(mm, _adj_rec(0.9), "4_I", p2, lambda l: 0)[0] == "discard"


def test_broad_candidate_accepts_korean_harmful_near_match():
    p2 = {"acceptance": {"broad_candidate": True, "min_taxonomy_fit_score": 0.45,
                           "min_korea_relevance_score": 0.3, "min_concrete_context_score": 0.2}}
    match = MatchResult(is_relevant=True, taxonomy_lv2="4_I", subtype="privacy_violation",
                        confidence=0.48, reason="", source="llm")
    action, reason = pipeline._phase2_adjudicate(match, _adj_rec(0.4, 0.48, 0.25), "4_I", p2, lambda l: 0)
    assert (action, reason) == ("accepted", "broad_candidate")


# ── small_run end-to-end (오프라인 stub) ──
def _stub_classify(predicted, subtype="privacy_violation", korea=0.9, fit=0.85, concrete=0.8,
                   relevant=True, lv1="Information and Safety Harms"):
    def classify(self, rec, policies, valid_pairs=None, taxo_lines=None, broad_candidate=False):
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


def _run(tmp_path, monkeypatch, classify, extract=_stub_extract, lv2="3_G_Misinformation_and_Disinformation", verify=True):
    monkeypatch.setattr(ExtractorRouter, "extract", extract)
    monkeypatch.setattr(LLMMatcher, "classify", classify)
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
    result = pipeline.verify_unverified_candidates(
        rep["run_id"], str(db), P2, taxonomy_config=TAXO,
    )
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT action,classification_source FROM content_records WHERE run_id=? LIMIT 1", (rep["run_id"],)
    ).fetchone()
    status = conn.execute(
        "SELECT status FROM url_candidates WHERE run_id=? AND status='trend_accepted' LIMIT 1", (rep["run_id"],)
    ).fetchone()
    conn.close()
    assert result["verified"] >= 1 and result["accepted"] >= 1
    assert row == ("accepted", "llm") and status == ("trend_accepted",)


def test_small_run_low_korea_excluded_not_stored(tmp_path, monkeypatch):
    db, rep = _run(tmp_path, monkeypatch, _stub_classify("3_G_Misinformation_and_Disinformation", korea=0.2))
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT COUNT(*) FROM content_records WHERE action='accepted'").fetchone()[0]
    excluded = conn.execute(
        "SELECT COUNT(*) FROM url_candidates WHERE status='trend_discard'").fetchone()[0]
    conn.close()
    assert stored == 0 and excluded >= 1


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


def test_small_run_opportunistic_mismatch_stored(tmp_path, monkeypatch):
    # target 4_I 로 검색했으나 LLM은 2_F로 판정 → 2_F 부족(빈 db)이므로 opportunistic accepted
    db, rep = _run(tmp_path, monkeypatch,
                   _stub_classify("2_F_Bias_and_Hate", subtype="gender", lv1="Unfair Representation"))
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT taxonomy_lv2_candidate, taxonomy_lv2 FROM content_records WHERE action='accepted' LIMIT 1"
    ).fetchone()
    conn.close()
    assert row == ("3_G_Misinformation_and_Disinformation", "2_F_Bias_and_Hate")   # target != predicted 분리 저장


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
    return pipeline.small_run(["3_G_Misinformation_and_Disinformation"], limit=6, config_path=P2,
                              db_path=str(db), taxonomy_config=TAXO,
                              provider=MockTavilyProvider(), **kw)


# ── 전략 경로(source_strategies_by_lv2) end-to-end ──
# 이 경로의 계약: OpenAI는 검색 계획에만 쓰고, 저장 여부는 결정론적 acceptance gate가 정한다.
from src.pipelines import gap_filling as _gf                       # noqa: E402
from src.phase2.query_planner import QueryPlan, QueryPlanner       # noqa: E402

CBRNE_BODY = (
    "환경부와 소방청은 경기도 화성시 사업장에서 유해화학물질이 누출되는 사고가 발생해 주민 대피와 "
    "함께 조사에 착수했다고 밝혔다. 소방 당국은 누출 물질을 확인하고 인근 주민 피해 여부를 조사 중이다. "
    "경찰도 사업장 관리 책임에 대한 수사에 나섰다.") * 3


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
        "modes": ["official_seed", "search_expand"], "recency_days": 730, "lv2_store_target": 30,
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
                  extract=_stub_cbrne_extract, limit=6):
    """전략 경로 실행. planner와 source discovery만 갈아끼우고 나머지는 실제 코드를 탄다."""
    strategy = strategy or _cbrne_strategy()
    results_by_source = results_by_source or {
        "fire_agency": [_result("https://www.nfa.go.kr/notice/1", "fire_agency")],
        "web": [_result("https://www.yna.co.kr/view/1", "web")],
    }
    p2 = yaml.safe_load(open(P2, encoding="utf-8"))
    p2["source_strategies_by_lv2"] = {"6_O_CBRNE": strategy}
    monkeypatch.setattr(_gf, "_load_phase2_config", lambda path: p2)

    calls = {"plan": 0, "classify": 0, "extract": []}

    def fake_plan(self, lv2, definition, strat, seeds, low=None, query_kind=""):
        calls["plan"] += 1
        calls.setdefault("seeds", []).append(list(seeds))
        return [_plan(s["id"]) for s in strat["sources"]]

    def fake_discover(plans, source, strat, lv2, ctx):
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
                                taxonomy_config=TAXO, provider=MockTavilyProvider())
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


def test_strategy_run_rejects_records_without_korea_or_lv2_evidence(tmp_path, monkeypatch):
    def foreign_extract(self, c, collected_at, task=None):
        body = ("일본 후쿠시마 인근 공장에서 유해화학물질이 누출되는 사고가 났다고 현지 언론이 보도했다. ") * 8
        outcome = _stub_cbrne_extract(self, c, collected_at, task)
        outcome.record.title = "후쿠시마 누출 사고"
        outcome.record.body_text = outcome.record.raw_text = body
        return outcome

    db, _, _ = _run_strategy(tmp_path, monkeypatch, extract=foreign_extract,
                             results_by_source={"web": [_result("https://example.com/a", "web")]})
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM content_records").fetchone()[0] == 0
    reasons = [r[0] for r in conn.execute(
        "SELECT reason FROM filter_logs WHERE stage='phase2_acceptance'")]
    conn.close()
    assert reasons and all(r.startswith("not_domestic_direct") for r in reasons)


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


def test_lv2_store_target_stops_the_loop(tmp_path, monkeypatch):
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

    strategy = _cbrne_strategy(modes=["search_planned"], lv2_store_target=2, sources=[
        {"id": "web", "access": "direct", "method": "web_search", "priority": 1}])
    db, _, calls = _run_strategy(tmp_path, monkeypatch, strategy=strategy,
                                 results_by_source={"web": results}, extract=city_extract)
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT COUNT(*) FROM content_records").fetchone()[0]
    conn.close()
    # 목표 저장량에 도달하면 남은 후보를 더 fetch하지 않는다.
    assert stored == 2 and len(calls["extract"]) < len(CITIES)


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
        assert strategy["recency_days"] > 0 and strategy["lv2_store_target"] > 0
        for source in strategy["sources"]:
            assert source["method"] in known_methods, f"{lv2}/{source['id']}: 미지원 method"
            assert source["access"] in {"direct", "discovery_only", "metadata_only", "blocked"}
            if source["method"] in {"official_board", "serpapi_site"}:
                assert source.get("domain"), f"{lv2}/{source['id']}: site: 검색에 domain 필요"
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
