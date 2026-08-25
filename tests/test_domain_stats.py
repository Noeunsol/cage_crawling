"""도메인별 추출 성공/실패 통계 (kin.naver.com처럼 미검증 도메인을 판단할 근거)."""

from src.storage import database
from src.storage.repositories import contents as contents_repo
from src.storage.repositories import discarded as discarded_repo
from src.storage.repositories import domain_stats


def _conn(tmp_path):
    return database.connect(tmp_path / "test.db")


def test_domain_stats_with_no_history_has_no_rate(tmp_path):
    conn = _conn(tmp_path)
    stats = domain_stats.get_domain_stats(conn, "kin.naver.com")
    assert stats.total == 0
    assert stats.success_rate is None


def test_domain_stats_combines_contents_and_extraction_failures(tmp_path):
    conn = _conn(tmp_path)
    for i in range(8):
        contents_repo.upsert_content(
            conn, title=f"t{i}", content=f"c{i}", published_date=None,
            canonical_url=f"https://kin.naver.com/{i}", source_name=None,
            source_domain="kin.naver.com", source_category=None,
            status="accepted", content_hash=f"h{i}",
        )
    for i in range(2):
        discarded_repo.record_discarded(
            conn, original_url=f"https://kin.naver.com/fail{i}", normalized_url=f"https://kin.naver.com/fail{i}",
            run_id=None, query_id=None, source_domain="kin.naver.com",
            reason="extraction_failed", retryable=True,
        )

    stats = domain_stats.get_domain_stats(conn, "kin.naver.com")

    assert stats.success == 8
    assert stats.extraction_failed == 2
    assert stats.total == 10
    assert stats.success_rate == 0.8


def test_domain_stats_ignores_non_extraction_discard_reasons(tmp_path):
    conn = _conn(tmp_path)
    discarded_repo.record_discarded(
        conn, original_url="https://x.com/1", normalized_url="https://x.com/1",
        run_id=None, query_id=None, source_domain="x.com", reason="duplicate", retryable=False,
    )
    stats = domain_stats.get_domain_stats(conn, "x.com")
    assert stats.extraction_failed == 0
    assert stats.total == 0


def test_list_all_domain_stats_covers_every_domain_seen(tmp_path):
    conn = _conn(tmp_path)
    contents_repo.upsert_content(
        conn, title="t", content="c", published_date=None, canonical_url="https://good.com/1",
        source_name=None, source_domain="good.com", source_category=None,
        status="accepted", content_hash="h1",
    )
    discarded_repo.record_discarded(
        conn, original_url="https://bad.com/1", normalized_url="https://bad.com/1",
        run_id=None, query_id=None, source_domain="bad.com", reason="extraction_failed", retryable=True,
    )

    stats_by_domain = {s.domain: s for s in domain_stats.list_all_domain_stats(conn)}

    assert stats_by_domain["good.com"].success == 1
    assert stats_by_domain["good.com"].success_rate == 1.0
    assert stats_by_domain["bad.com"].extraction_failed == 1
    assert stats_by_domain["bad.com"].success_rate == 0.0
