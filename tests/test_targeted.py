"""2차 semantic targeted discovery 검증 (오프라인, assert 기반).

coverage/intent/provider/reranker 단위 + small_run end-to-end(provider/extract/LLM monkeypatch).
불변식: API content_hint는 본문 저장 금지, 추출 실패 시 미저장, korea 낮으면 excluded,
target/predicted 분리, provenance(run_id/phase=2), opportunistic 판정.
"""
import sqlite3

from src import pipeline
from src.reporting import coverage
from src.extract import ExtractorRouter
from src.extract.base import ExtractionOutcome
from src.classify.matcher import LLMMatcher
from src.phase2.intent_builder import build_collection_intent
from src.phase2.provider import MockTavilyProvider, SearchResult, to_candidate
from src.phase2.reranker import rerank
from src.policy import load_policies
from src.schema import ContentRecord, MatchResult
from src.site_registry import SiteRegistry

TAXO = "configs/taxonomy.yaml"
P2 = "configs/phase2_semantic_collection.yaml"
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


# ── 단위: intent (label 미포함, exclude 반영, sensitive_overlay) ──
def test_intent_builder_no_label_and_sensitive_overlay():
    policies = {p.taxonomy_lv2: p for p in load_policies(TAXO)}
    cfg = {
        "collection_intents_by_lv2": {"4_I_Privacy_Infringement": {
            "goal": "개인정보 유출 피해 콘텐츠를 찾는다", "include": ["신상털이"],
            "exclude": ["개인정보보호법 단순 설명"], "korea_required": True}},
        "sensitive_overlay": {"6_O_CBRNE": {
            "allowed_content_types": ["news_case"], "disallowed_intents": ["manufacturing"],
            "force_review": True}},
        "providers": {"tavily": {"max_results_per_query": 5}},
    }
    m = build_collection_intent(policies["4_I_Privacy_Infringement"], cfg)
    assert "4_I_Privacy_Infringement" not in m.natural_language_query
    assert "신상털이" in m.natural_language_query and "개인정보보호법 단순 설명" in m.natural_language_query
    s = build_collection_intent(policies["6_O_CBRNE"], cfg)
    assert s.force_review and "news_case" in s.natural_language_query
    assert any("manufacturing" in e for e in s.exclude)


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


# ── 단위: adjudicate (korea 게이트 + opportunistic) ──
def _adj_rec(korea, fit=0.9, concrete=0.9):
    r = ContentRecord(source_url="u", domain="d", site_name="s", site_type="community",
                      taxonomy_lv2_candidate="4_I", subtype_candidate="", title="t", body_text="b",
                      collected_at="2026", search_query="q", search_api="tavily", extractor="x")
    r.korea_relevance_score, r.taxonomy_fit_score, r.concrete_context_score = korea, fit, concrete
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


# ── small_run end-to-end (오프라인 stub) ──
def _stub_classify(predicted, subtype="privacy_violation", korea=0.9, fit=0.85, concrete=0.8,
                   relevant=True, lv1="Information and Safety Harms"):
    def classify(self, rec, policies, valid_pairs=None, taxo_lines=None):
        rec.korea_relevance_score = korea
        rec.taxonomy_fit_score = rec.taxonomy_relevance_score = fit
        rec.concrete_context_score = concrete
        rec.harmfulness_score = 0.7
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


def _run(tmp_path, monkeypatch, classify, extract=_stub_extract, lv2="4_I_Privacy_Infringement"):
    monkeypatch.setattr(ExtractorRouter, "extract", extract)
    monkeypatch.setattr(LLMMatcher, "classify", classify)
    db = tmp_path / "p2.db"
    rep = pipeline.small_run([lv2], limit=6, config_path=P2, db_path=str(db),
                             taxonomy_config=TAXO, provider=MockTavilyProvider())
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
    conn.close()


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
