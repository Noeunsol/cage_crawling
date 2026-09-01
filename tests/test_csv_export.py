"""CSV export: data/final/[LV2]/[type].csv가 정확히 생성되고, URL/콘텐츠 중복이 없다."""

import csv

from src.storage import database
from src.storage.csv_exporter import CSV_COLUMNS, accepted_type_pairs, export_all, export_run, export_type
from src.storage.repositories import contents as contents_repo
from src.storage.repositories import discoveries as discoveries_repo
from src.storage.repositories import queries as queries_repo
from src.storage.repositories import runs as runs_repo
from src.storage.repositories import taxonomy_mappings as mappings_repo
from src.utils.text import compute_content_hash


def _add_content(conn, *, url, title, content, status="accepted", source_domain="example.com",
                  source_category="news", source_name="예시뉴스", published_date="2025-06-01"):
    content_id, _ = contents_repo.upsert_content(
        conn, title=title, content=content, published_date=published_date,
        canonical_url=url, source_name=source_name, source_domain=source_domain,
        source_category=source_category, status=status,
        content_hash=compute_content_hash(title, content),
    )
    return content_id


def _read_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def test_export_type_writes_expected_columns_and_only_accepted(tmp_path):
    conn = database.connect(tmp_path / "test.db")
    final_dir = tmp_path / "final"

    accepted_id = _add_content(conn, url="https://a.com/1", title="제목1", content="본문1")
    mappings_repo.add_mapping(
        conn, content_id=accepted_id, taxonomy_lv2="1_C_Self_Harm", type_name="suicide",
        decision="accepted", decision_reason=None, prompt_name=None, prompt_version=None, model=None,
    )

    excluded_id = _add_content(conn, url="https://a.com/2", title="제목2", content="본문2", status="excluded")
    mappings_repo.add_mapping(
        conn, content_id=excluded_id, taxonomy_lv2="1_C_Self_Harm", type_name="suicide",
        decision="excluded", decision_reason="taxonomy_mismatch", prompt_name=None, prompt_version=None, model=None,
    )

    result = export_type(conn, "1_C_Self_Harm", "suicide", final_dir)

    assert result.path == final_dir / "1_C_Self_Harm" / "suicide.csv"
    assert result.row_count == 1
    rows = _read_csv(result.path)
    assert len(rows) == 1
    assert list(rows[0].keys()) == CSV_COLUMNS
    assert rows[0]["title"] == "제목1"
    assert rows[0]["url"] == "https://a.com/1"
    assert rows[0]["date"] == "2025-06-01"


def test_export_dedups_by_content_hash_within_type(tmp_path):
    conn = database.connect(tmp_path / "test.db")
    final_dir = tmp_path / "final"

    for url in ["https://a.com/1", "https://a.com/1-mirror"]:
        cid = _add_content(conn, url=url, title="같은 제목", content="같은 본문")
        mappings_repo.add_mapping(
            conn, content_id=cid, taxonomy_lv2="1_C_Self_Harm", type_name="suicide",
            decision="accepted", decision_reason=None, prompt_name=None, prompt_version=None, model=None,
        )

    result = export_type(conn, "1_C_Self_Harm", "suicide", final_dir)
    assert result.row_count == 1  # content_hash가 같아 한 번만 들어간다


def test_same_content_can_appear_in_multiple_type_csvs(tmp_path):
    conn = database.connect(tmp_path / "test.db")
    final_dir = tmp_path / "final"

    cid = _add_content(conn, url="https://a.com/1", title="제목", content="본문")
    mappings_repo.add_mapping(
        conn, content_id=cid, taxonomy_lv2="1_C_Self_Harm", type_name="suicide",
        decision="accepted", decision_reason=None, prompt_name=None, prompt_version=None, model=None,
    )
    mappings_repo.add_mapping(
        conn, content_id=cid, taxonomy_lv2="1_C_Self_Harm", type_name="self_injury",
        decision="accepted", decision_reason=None, prompt_name=None, prompt_version=None, model=None,
    )

    results = export_all(conn, final_dir, targets=[
        ("1_C_Self_Harm", "suicide"), ("1_C_Self_Harm", "self_injury"),
    ])

    assert {r.row_count for r in results} == {1}
    assert len(_read_csv(final_dir / "1_C_Self_Harm" / "suicide.csv")) == 1
    assert len(_read_csv(final_dir / "1_C_Self_Harm" / "self_injury.csv")) == 1


def test_reexport_overwrites_instead_of_appending(tmp_path):
    conn = database.connect(tmp_path / "test.db")
    final_dir = tmp_path / "final"

    cid1 = _add_content(conn, url="https://a.com/1", title="제목1", content="본문1")
    mappings_repo.add_mapping(
        conn, content_id=cid1, taxonomy_lv2="1_C_Self_Harm", type_name="suicide",
        decision="accepted", decision_reason=None, prompt_name=None, prompt_version=None, model=None,
    )
    first = export_type(conn, "1_C_Self_Harm", "suicide", final_dir)
    assert first.row_count == 1

    cid2 = _add_content(conn, url="https://a.com/2", title="제목2", content="본문2")
    mappings_repo.add_mapping(
        conn, content_id=cid2, taxonomy_lv2="1_C_Self_Harm", type_name="suicide",
        decision="accepted", decision_reason=None, prompt_name=None, prompt_version=None, model=None,
    )
    second = export_type(conn, "1_C_Self_Harm", "suicide", final_dir)

    assert second.row_count == 2
    rows = _read_csv(second.path)
    assert len(rows) == 2  # 이전 실행 결과가 남아 3개가 되지 않고 최신 상태로 덮어써진다


def test_accepted_type_pairs_discovers_targets(tmp_path):
    conn = database.connect(tmp_path / "test.db")
    cid = _add_content(conn, url="https://a.com/1", title="제목", content="본문")
    mappings_repo.add_mapping(
        conn, content_id=cid, taxonomy_lv2="1_C_Self_Harm", type_name="suicide",
        decision="accepted", decision_reason=None, prompt_name=None, prompt_version=None, model=None,
    )
    assert accepted_type_pairs(conn) == [("1_C_Self_Harm", "suicide")]


def test_export_run_keeps_existing_csv_and_adds_only_selected_run(tmp_path):
    conn = database.connect(tmp_path / "test.db")
    final_dir = tmp_path / "final"
    lv2_id, type_name = "1_C_Self_Harm", "suicide"

    existing_id = _add_content(conn, url="https://a.com/existing", title="기존", content="기존 본문")
    mappings_repo.add_mapping(
        conn, content_id=existing_id, taxonomy_lv2=lv2_id, type_name=type_name,
        decision="accepted", decision_reason=None, prompt_name=None, prompt_version=None, model=None,
    )
    export_type(conn, lv2_id, type_name, final_dir)

    for run_id, url, title in [
        ("run-selected", "https://a.com/selected", "선택 결과"),
        ("run-other", "https://a.com/other", "다른 실행 결과"),
    ]:
        runs_repo.create_run(conn, run_id, {})
        query_id = queries_repo.create_query(
            conn, taxonomy_lv2=lv2_id, type_name=type_name, provider="tavily",
            query_text=run_id, status="used", created_by="user",
        )
        content_id = _add_content(conn, url=url, title=title, content=f"{title} 본문")
        mappings_repo.add_mapping(
            conn, content_id=content_id, taxonomy_lv2=lv2_id, type_name=type_name,
            decision="accepted", decision_reason=None, prompt_name=None, prompt_version=None, model=None,
        )
        discoveries_repo.record_discovery(
            conn, content_id=content_id, run_id=run_id, query_id=query_id,
            provider="tavily", returned_url=url,
        )

    export_run(conn, "run-selected", final_dir)
    export_run(conn, "run-selected", final_dir)  # 같은 run을 다시 눌러도 중복되지 않는다.

    rows = _read_csv(final_dir / lv2_id / f"{type_name}.csv")
    assert {row["title"] for row in rows} == {"기존", "선택 결과"}
