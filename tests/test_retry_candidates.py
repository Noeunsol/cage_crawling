"""find_retryable_candidates가 reason별로 attempt_count를 세는지 검증."""

from src.discovery.retry_candidates import find_retryable_candidates
from src.storage import database
from src.storage.repositories import queries as queries_repo

CONFIGS = {
    "retry_policy": {
        "reasons": {
            "timeout": {"max_attempts": 2},
            "temporary_http_error": {"max_attempts": 1},
        }
    }
}


def _make_conn(tmp_path):
    return database.connect(tmp_path / "test.db")


def _seed_discard(conn, query_id, url, reason):
    conn.execute(
        """
        INSERT INTO discarded_candidates
            (original_url, normalized_url, query_id, reason, retryable)
        VALUES (?, ?, ?, ?, 1)
        """,
        (url, url, query_id, reason),
    )
    conn.commit()


def test_attempt_count_is_scoped_by_reason(tmp_path):
    """같은 URL이 다른 reason으로도 discard됐다고 해서, 특정 reason의 max_attempts가
    다른 reason의 시도 횟수까지 합산해 조기 소진되면 안 된다."""
    conn = _make_conn(tmp_path)
    query_id = queries_repo.create_query(
        conn, taxonomy_lv2="1_A_Toxic_Language", type_name="cyberstalking",
        provider="tavily", query_text="q", status="used", created_by="test",
    )
    url = "https://example.com/a"

    # 가장 최근 discard는 timeout(1번째, max_attempts=2 미달이라 재시도 대상이어야 함).
    # 그 전에 temporary_http_error로도 1번 discard된 적이 있다 — reason을 구분하지 않고
    # 합산하면 attempt_count=2가 timeout의 max_attempts=2에 걸려 잘못 제외된다.
    _seed_discard(conn, query_id, url, "temporary_http_error")
    _seed_discard(conn, query_id, url, "timeout")

    candidates = find_retryable_candidates(
        conn, CONFIGS, targets=[("1_A_Toxic_Language", "cyberstalking")],
    )

    assert len(candidates) == 1
    assert candidates[0].url == url
