"""1차·2차 공용 foundation 테스트 — 저장·중복·마이그레이션·정제·리포트.

모드별 동작은 test_trend.py(1차) / test_targeted.py(2차)가 본다.
여기는 두 모드가 함께 쓰는 src/common 계층만 검증한다.
"""
import sqlite3

from src.common.clean import clean_record
from src.common.report import build_report
from src.common.schema import ContentRecord, UrlCandidate
from src.common.storage.dedup import hamming, simhash
from src.common.storage.store import Store


def _record(url="https://example.com/1"):
    return ContentRecord(url, "example.com", "unknown", "unknown", "T", "S", "제목",
                         "악플 피해 사례 " * 50, "2026-01-01", "q", "mock", "trafilatura")


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
    statuses = ["discovered", "url_filtered", "budget_exceeded", "extraction_failed",
                "extracted", "quality_failed", "accepted", "discard", "candidate",
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
                     collection_type="news_case", discovery_method="tavily", status="accepted")
    store.save_candidate(c)
    report = build_report(store)
    assert report["collection_type_conversion"]["news_case"]["accepted"] == 1
    assert report["api_conversion"]["tavily"]["conversion_rate"] == 1.0
    store.close()


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
    row = store.conn.execute("SELECT candidate_id,source_url FROM url_candidates").fetchone()
    assert row[0] and row[1] == "https://example.com/old"
    store.close()


def test_trend_prefixed_status_is_migrated_to_neutral(tmp_path):
    """v23: 2차 레코드에까지 붙던 trend_ 접두어를 걷어낸다(1차/2차 구분은 collection_phase가 한다)."""
    path = tmp_path / "legacy.db"
    store = Store(str(path)); store.close()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO url_candidates (candidate_id,source_url,status) VALUES ('c1','u1','trend_accepted')")
    conn.execute("INSERT INTO url_candidates (candidate_id,source_url,status) VALUES ('c2','u2','trend_discard')")
    conn.execute("INSERT INTO url_candidates (candidate_id,source_url,status) VALUES ('c3','u3','duplicate')")
    conn.execute("PRAGMA user_version = 22")
    conn.commit(); conn.close()

    store = Store(str(path))
    got = sorted(x[0] for x in store.conn.execute("SELECT status FROM url_candidates"))
    assert got == ["accepted", "discard", "duplicate"]   # 건수 보존 + 접두어 제거
    store.close()


def test_cleaning_preserves_harmful_expression():
    rec = _record()
    rec.raw_text = "연락 010-1234-5678 token=abcdefghijk 악플"
    clean_record(rec)
    # 욕설·협박은 보존한다. 정제는 boilerplate/노이즈만 건드린다. PII 마스킹도 하지 않는다.
    assert rec.cleaned_text == rec.raw_text == rec.body_text


def test_simhash_near_dup():
    base = ("온라인 커뮤니티에서 특정 이용자를 겨냥한 악플과 좌표찍기가 반복되며 조리돌림으로 번졌다 "
            "피해자는 게시글과 댓글을 캡처해 명예훼손으로 고소를 준비 중이라고 밝혔다 "
            "운영진은 신고된 게시글을 삭제했지만 이미 여러 채널로 확산된 뒤였다")
    near = base.replace("반복되며", "계속 반복되며").replace("삭제했지만", "일부 삭제했지만")
    far = "오늘 점심으로 김치찌개를 먹었는데 맛집이라 사람이 많았고 대기 시간이 길었다 다음엔 예약하고 가야겠다"
    # near-dup은 무관 문서보다 훨씬 가까움 (SimHash 판별력)
    assert hamming(simhash(base), simhash(near)) < hamming(simhash(base), simhash(far))
    assert hamming(simhash(base), simhash(base)) == 0
