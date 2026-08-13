"""2차 semantic targeted discovery 검증 (오프라인, assert 기반).

coverage/intent/provider/reranker 단위 + small_run end-to-end(provider/extract/LLM monkeypatch).
불변식: API content_hint는 본문 저장 금지, 추출 실패 시 미저장, korea 낮으면 excluded,
target/predicted 분리, provenance(run_id/phase=2), opportunistic 판정.
"""
import sqlite3

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
from src.phase2.provider import MockTavilyProvider, SearchResult, to_candidate
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


def test_config_budget_matches_written_queries():
    """손으로 쓴 쿼리가 max_searches에 걸려 버려지고 있지 않은지. config 드리프트 감지용."""
    cfg = yaml.safe_load(open(P2, encoding="utf-8"))
    for policy in load_policies(TAXO):
        intent = build_collection_intent(policy, cfg)
        assert not intent.dropped_queries, (
            f"{policy.taxonomy_lv2}: max_searches={intent.max_searches}인데 "
            f"쿼리 {len(intent.dropped_queries)}개가 매 실행마다 버려짐")


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


def _run(tmp_path, monkeypatch, classify, extract=_stub_extract, lv2="4_I_Privacy_Infringement", verify=True):
    monkeypatch.setattr(ExtractorRouter, "extract", extract)
    monkeypatch.setattr(LLMMatcher, "classify", classify)
    db = tmp_path / "p2.db"
    rep = pipeline.small_run([lv2], limit=6, config_path=P2, db_path=str(db),
                             taxonomy_config=TAXO, provider=MockTavilyProvider(),
                             overrides={"adjudication": {"openai_verification": verify}})
    return db, rep


def test_small_run_accepted_provenance_and_hint_isolation(tmp_path, monkeypatch):
    db, rep = _run(tmp_path, monkeypatch, _stub_classify("4_I_Privacy_Infringement"))
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT taxonomy_lv2_candidate, taxonomy_lv2, action, collection_phase, run_id, "
        "discovery_provider, body_text FROM content_records WHERE action='accepted'").fetchall()
    assert rows, "accepted 최소 1건"
    tgt, pred, action, phase, run_id, prov, body = rows[0]
    assert tgt == "4_I_Privacy_Infringement" and pred == "4_I_Privacy_Infringement"
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


def test_small_run_without_openai_stores_unverified_tavily_candidate(tmp_path, monkeypatch):
    db, _ = _run(tmp_path, monkeypatch, _stub_classify("4_I_Privacy_Infringement"), verify=False)
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT action,classification_source FROM content_records LIMIT 1").fetchone()
    conn.close()
    assert row == ("candidate", "tavily_unverified")


def test_reverify_existing_tavily_candidate_with_openai(tmp_path, monkeypatch):
    db, rep = _run(tmp_path, monkeypatch, _stub_classify("4_I_Privacy_Infringement"), verify=False)
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
    db, rep = _run(tmp_path, monkeypatch, _stub_classify("4_I_Privacy_Infringement", korea=0.2))
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT COUNT(*) FROM content_records WHERE action='accepted'").fetchone()[0]
    excluded = conn.execute(
        "SELECT COUNT(*) FROM url_candidates WHERE status='trend_discard'").fetchone()[0]
    conn.close()
    assert stored == 0 and excluded >= 1


def test_small_run_extraction_failure_not_stored(tmp_path, monkeypatch):
    def fail_extract(self, c, collected_at, task=None):
        return ExtractionOutcome(record=None, tried=["all_fail"], reason="all_extractors_failed")
    db, rep = _run(tmp_path, monkeypatch, _stub_classify("4_I_Privacy_Infringement"), extract=fail_extract)
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
    assert row == ("4_I_Privacy_Infringement", "2_F_Bias_and_Hate")   # target != predicted 분리 저장


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
    monkeypatch.setattr(LLMMatcher, "classify", _stub_classify("4_I_Privacy_Infringement"))
    return pipeline.small_run(["4_I_Privacy_Infringement"], limit=6, config_path=P2,
                              db_path=str(db), taxonomy_config=TAXO,
                              provider=MockTavilyProvider(), **kw)
