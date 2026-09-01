"""SerpAPI 도메인 번들 상태(SQLite) 검증 — sync/LRU pick/mark_used/enabled 토글."""

from src.storage import database
from src.storage.repositories import domain_bundles as bundles_repo


def _conn(tmp_path):
    return database.connect(tmp_path / "test.db")


def test_sync_creates_bundles_matching_pure_algorithm(tmp_path):
    conn = _conn(tmp_path)
    bundles_repo.sync_bundles(conn, "suicide", ["d1", "d2", "d3", "d4"], alias_groups={})

    rows = bundles_repo.list_bundles(conn, "suicide")
    assert [r["bundle_index"] for r in rows] == [0, 1, 2, 3]
    assert bundles_repo.pick_bundle(conn, "suicide").domains == ["d1", "d2", "d3"]


def test_sync_is_idempotent_and_preserves_last_used_at(tmp_path):
    conn = _conn(tmp_path)
    bundles_repo.sync_bundles(conn, "suicide", ["d1", "d2"], alias_groups={})
    bundles_repo.mark_used(conn, "suicide", 0)
    used_at = bundles_repo.list_bundles(conn, "suicide")[0]["last_used_at"]
    assert used_at is not None

    bundles_repo.sync_bundles(conn, "suicide", ["d1", "d2"], alias_groups={})  # 재실행

    assert bundles_repo.list_bundles(conn, "suicide")[0]["last_used_at"] == used_at


def test_sync_removes_stale_bundle_indices_when_domain_count_shrinks(tmp_path):
    conn = _conn(tmp_path)
    bundles_repo.sync_bundles(conn, "suicide", ["d1", "d2", "d3", "d4"], alias_groups={})
    assert len(bundles_repo.list_bundles(conn, "suicide")) == 4

    bundles_repo.sync_bundles(conn, "suicide", ["d1"], alias_groups={})

    assert len(bundles_repo.list_bundles(conn, "suicide")) == 1


def test_pick_bundle_prefers_never_used_then_least_recently_used(tmp_path):
    conn = _conn(tmp_path)
    bundles_repo.sync_bundles(conn, "suicide", ["d1", "d2", "d3", "d4"], alias_groups={})

    first = bundles_repo.pick_bundle(conn, "suicide")
    assert first.bundle_index == 0   # 둘 다 안 쓰였으면 index 순서(결정론적)
    bundles_repo.mark_used(conn, "suicide", first.bundle_index)

    second = bundles_repo.pick_bundle(conn, "suicide")
    assert second.bundle_index == 1  # 아직 안 쓰인 나머지가 우선

    bundles_repo.mark_used(conn, "suicide", second.bundle_index)
    third = bundles_repo.pick_bundle(conn, "suicide")
    assert third.bundle_index == 2   # 아직 사용하지 않은 교차 조합이 계속 우선된다
    bundles_repo.mark_used(conn, "suicide", third.bundle_index)

    fourth = bundles_repo.pick_bundle(conn, "suicide")
    assert fourth.bundle_index == 3
    bundles_repo.mark_used(conn, "suicide", fourth.bundle_index)

    assert bundles_repo.pick_bundle(conn, "suicide").bundle_index == 0


def test_disabled_bundle_is_skipped_by_pick(tmp_path):
    conn = _conn(tmp_path)
    bundles_repo.sync_bundles(conn, "suicide", ["d1", "d2", "d3", "d4"], alias_groups={})
    bundles_repo.set_enabled(conn, "suicide", 0, False)

    assert bundles_repo.pick_bundle(conn, "suicide").bundle_index == 1


def test_no_enabled_bundles_returns_none(tmp_path):
    conn = _conn(tmp_path)
    bundles_repo.sync_bundles(conn, "suicide", ["d1"], alias_groups={})
    bundles_repo.set_enabled(conn, "suicide", 0, False)

    assert bundles_repo.has_enabled_bundle(conn, "suicide") is False
    assert bundles_repo.pick_bundle(conn, "suicide") is None
