import sqlite3

from src.clean import clean_record
from src.discovery import DiscoveryRouter
from src.extract import ExtractorRouter
from src.mask import apply_preservation_policy
from src.classify.matcher import RuleBasedMatcher
from src.keyword_discovery.strategy import Budget, StrategyRouter, StrategyTask
from src.keyword_discovery.frontier import UrlFrontier
from src.pipeline import _classification_status
from src.policy import Subtype
from src.keyword_discovery.query import QueryGenerator
from src.reporting.report import build_report
from src.schema import ContentRecord, MatchResult, UrlCandidate
from src.site_registry import SiteRegistry
from src.storage.store import Store


def _registry():
    return SiteRegistry.load("configs/site_policy.yaml")


def _record(url="https://example.com/1"):
    return ContentRecord(url, "example.com", "unknown", "unknown", "T", "S", "제목",
                         "악플 피해 사례 " * 50, "2026-01-01", "q", "mock", "trafilatura")


def test_subtype_builds_single_task_from_collection_type():
    subtype = Subtype(name="S", collection_type="news_case")
    tasks = StrategyRouter().build_tasks("T", subtype)
    assert len(tasks) == 1
    assert tasks[0].collection_type == "news_case"
    # primary_methods 미지정 → collection_type 기본 체인 상속
    assert tasks[0].discovery_methods == ["rss", "sitemap", "tavily", "serpapi_site"]


def test_preservation_policy_masks_pii_and_credentials_by_default():
    task = StrategyRouter().build_tasks("T", Subtype(name="S"))[0]
    masked = apply_preservation_policy("연락 010-1234-5678 api_key=abcdefghijk", task.preservation_policy)
    assert "[PHONE]" in masked and "[SECRET]" in masked


def test_preferred_extractor_changes_ladder():
    router = ExtractorRouter(_registry(), {"extraction": {}})
    task = StrategyTask("T", "S", "news_case", preferred_extractors=["playwright"])
    ladder = router._ladder(_registry().lookup("news.naver.com"), task)
    assert ladder.index("playwright") < ladder.index("trafilatura")


def test_provider_failure_continues_to_next_discovery_method(monkeypatch):
    class Failing:
        name = "serpapi"
        def search(self, *args):
            raise RuntimeError("down")
    from src.keyword_discovery import search
    monkeypatch.setitem(search._CLIENTS, "serpapi", Failing())
    task = StrategyTask("T", "S", "news_case", discovery_methods=["serpapi_site", "tavily"])
    found = DiscoveryRouter(_registry(), QueryGenerator(_registry()), Budget({"global_max_queries": 10})).discover(
        task, Subtype(name="S", keywords=["악플"]))
    assert found and all(x.discovery_method == "tavily" for x in found)


def test_reference_page_is_detected():
    c = UrlCandidate("https://namu.wiki/w/test", "namu.wiki", "q", "tavily", "T", "S", title="용어 정의")
    frontier = UrlFrontier(_registry())
    frontier.add(c)
    assert frontier.is_reference(c)


def test_irrelevant_llm_result_cannot_pass():
    rec = _record(); rec.taxonomy_fit_score = rec.harmfulness_score = rec.seed_source_value_score = 0.99
    rec.pii_risk_score = 0
    match = MatchResult(False, "T", "S", 0.99, "llm:not relevant")
    assert _classification_status(match, rec, {}, 1.0) == "fail"


def test_max_urls_per_query_is_applied():
    task = StrategyTask("T", "S", "news_case", discovery_methods=["tavily"])
    found = DiscoveryRouter(_registry(), QueryGenerator(_registry()), Budget(), max_urls_per_query=1).discover(
        task, Subtype(name="S", keywords=["악플"]))
    assert len(found) == 1


def test_db_persistent_near_duplicate(tmp_path):
    store = Store(str(tmp_path / "x.db"))
    first = _record(); first.taxonomy_lv2 = "T"; first.subtype = "S"
    first.canonical_url = first.source_url; first.dedup_hash = "a"; first.simhash = "42"; first.event_key = "e"
    store.save_content(first)
    second = _record("https://example.com/2"); second.taxonomy_lv2 = "T"; second.subtype = "S"
    second.canonical_url = second.source_url; second.dedup_hash = "b"; second.simhash = "42"; second.event_key = "f"
    assert store.find_duplicate(second, 3) == first.content_id
    store.close()


def test_all_candidate_statuses_are_persistable(tmp_path):
    store = Store(str(tmp_path / "x.db"))
    statuses = ["discovered", "url_filtered", "pending_budget_exceeded", "extraction_failed",
                "extracted", "quality_failed", "matched_pass", "matched_review", "matched_fail",
                "duplicate", "reference_only"]
    for i, status in enumerate(statuses):
        c = UrlCandidate(f"https://example.com/{i}", "example.com", "q", "mock", "T", "S", status=status)
        store.save_candidate(c)
    got = {x[0] for x in store.conn.execute("SELECT status FROM url_candidates")}
    assert got == set(statuses)
    store.close()


def test_report_contains_collection_type_and_api_conversion(tmp_path):
    store = Store(str(tmp_path / "x.db"))
    c = UrlCandidate("https://example.com/1", "example.com", "q", "tavily", "T", "S",
                     collection_type="news_case", discovery_method="tavily", status="matched_pass")
    store.save_candidate(c)
    report = build_report(store)
    assert report["collection_type_conversion"]["news_case"]["accepted"] == 1
    assert report["api_conversion"]["tavily"]["conversion_rate"] == 1.0
    store.close()


def test_non_api_discovery_does_not_consume_query_budget(monkeypatch):
    from src.fetcher import Fetcher
    html = """<tr class="ub-content us-post" data-no="2" data-type="icon_txt">
    <td class="gall_num">2</td>
    <td class="gall_tit"><a href="/board/view/?id=dcbest&amp;no=2">글</a></td>
    <td class="gall_date" title="2026-07-24 10:00:00">10:00</td></tr>"""
    monkeypatch.setattr(Fetcher, "fetch", lambda self, url: html)
    budget = Budget({"global_max_queries": 0})
    task = StrategyTask("T", "DCInside Smoke", "raw_expression", discovery_methods=["board_list"])
    subtype = Subtype(name="DCInside Smoke", priority_sites=["dcinside"])
    found = DiscoveryRouter(_registry(), QueryGenerator(_registry()), budget).discover(task, subtype)
    assert found


def test_same_url_discovery_history_is_preserved(tmp_path):
    store = Store(str(tmp_path / "x.db"))
    for method in ("serpapi_site", "tavily"):
        candidate = UrlCandidate("https://example.com/1", "example.com", "q", method, "T", "S",
                                 discovery_method=method)
        store.save_candidate(candidate)
    assert store.conn.execute("SELECT COUNT(*) FROM url_candidates").fetchone()[0] == 2
    store.close()


def test_old_candidate_table_is_migrated_without_data_loss(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE url_candidates (
           source_url TEXT PRIMARY KEY, domain TEXT, search_query TEXT, search_api TEXT,
           taxonomy_lv2_candidate TEXT, subtype_candidate TEXT, status TEXT, score REAL)"""
    )
    conn.execute(
        "INSERT INTO url_candidates VALUES (?,?,?,?,?,?,?,?)",
        ("https://example.com/old", "example.com", "q", "mock", "T", "S", "discovered", 0.5),
    )
    conn.commit(); conn.close()

    store = Store(str(path))
    row = store.conn.execute(
        "SELECT candidate_id,source_url FROM url_candidates"
    ).fetchone()
    assert row[0] and row[1] == "https://example.com/old"
    store.close()


def test_rule_matcher_uses_masked_comments():
    rec = _record()
    rec.masked_text = "일반 게시글"
    rec.masked_comments = ["온라인 커뮤니티에서 악플과 좌표찍기 피해가 발생했다"]
    subtype = Subtype(name="S", keywords=["악플", "좌표찍기"],
                      positive_patterns=["온라인 커뮤니티", "피해"])
    result = RuleBasedMatcher().match("T", subtype, rec)
    assert result.confidence >= 0.75


def test_taxonomy_policy_cannot_disable_global_pii_masking():
    rec = _record()
    rec.raw_text = "연락 010-1234-5678 token=abcdefghijk 악플"
    clean_record(rec, {"mask_pii": False, "mask_credentials": False})
    assert "[PHONE]" in rec.masked_text and "[SECRET]" in rec.masked_text
