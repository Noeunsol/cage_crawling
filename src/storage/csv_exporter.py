"""DB의 accepted 콘텐츠를 LV2/type별 CSV로 다시 만든다 (13절).

CSV에 직접 append하지 않는다 — export할 때마다 DB 기준으로 파일 전체를 새로 쓴다 (13.2절).
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from src.utils.text import compute_content_hash
from src.utils.urls import normalize_url

CSV_COLUMNS = ["title", "content", "date", "url", "source_domain"]


@dataclass
class ExportResult:
    lv2_id: str
    type_name: str
    path: Path
    row_count: int


def accepted_type_pairs(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """accepted 콘텐츠가 하나라도 있는 (LV2, type) 목록. export_all()의 기본 대상이 된다."""
    rows = conn.execute(
        "SELECT DISTINCT taxonomy_lv2, type_name FROM content_taxonomy_mappings WHERE decision = 'accepted'"
    ).fetchall()
    return [(r["taxonomy_lv2"], r["type_name"]) for r in rows]


def _accepted_rows(conn: sqlite3.Connection, lv2_id: str, type_name: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.* FROM contents c
        JOIN content_taxonomy_mappings m ON m.content_id = c.id
        WHERE m.taxonomy_lv2 = ? AND m.type_name = ?
          AND m.decision = 'accepted' AND c.status = 'accepted'
        ORDER BY c.published_date IS NULL, c.published_date, c.id
        """,
        (lv2_id, type_name),
    ).fetchall()


def _accepted_rows_by_run(
    conn: sqlite3.Connection, run_id: str, lv2_id: str, type_name: str,
) -> list[sqlite3.Row]:
    """선택한 실행에서 발견되어 해당 type으로 accepted된 콘텐츠만 반환한다."""
    return conn.execute(
        """
        SELECT DISTINCT c.* FROM content_discoveries cd
        JOIN search_queries q ON q.id = cd.query_id
        JOIN contents c ON c.id = cd.content_id
        JOIN content_taxonomy_mappings m
          ON m.content_id = c.id
         AND m.taxonomy_lv2 = q.taxonomy_lv2
         AND m.type_name = q.type_name
        WHERE cd.run_id = ? AND q.taxonomy_lv2 = ? AND q.type_name = ?
          AND m.decision = 'accepted' AND c.status = 'accepted'
        ORDER BY c.published_date IS NULL, c.published_date, c.id
        """,
        (run_id, lv2_id, type_name),
    ).fetchall()


def _csv_row(row) -> dict[str, str]:
    return {
        "title": row["title"],
        "content": row["content"],
        "date": row["published_date"] or "",
        "url": row["canonical_url"],
        "source_domain": row["source_domain"],
    }


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".csv.tmp")
    with open(temporary_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    temporary_path.replace(path)


def export_type(conn: sqlite3.Connection, lv2_id: str, type_name: str, final_dir: Path) -> ExportResult:
    """이 type CSV 하나를 통째로 다시 쓴다. 같은 content_hash는 한 번만 넣는다 (13.2절)."""
    seen_hashes: set[str] = set()
    unique_rows = []
    for row in _accepted_rows(conn, lv2_id, type_name):
        if row["content_hash"] in seen_hashes:
            continue
        seen_hashes.add(row["content_hash"])
        unique_rows.append(row)

    path = final_dir / lv2_id / f"{type_name}.csv"
    _write_csv(path, [_csv_row(row) for row in unique_rows])

    return ExportResult(lv2_id=lv2_id, type_name=type_name, path=path, row_count=len(unique_rows))


def export_all(
    conn: sqlite3.Connection, final_dir: Path, targets: list[tuple[str, str]] | None = None,
) -> list[ExportResult]:
    """targets를 안 주면 accepted 콘텐츠가 있는 모든 (LV2, type)을 대상으로 한다."""
    targets = accepted_type_pairs(conn) if targets is None else targets
    return [export_type(conn, lv2_id, type_name, final_dir) for lv2_id, type_name in targets]


def export_run(conn: sqlite3.Connection, run_id: str, final_dir: Path) -> list[ExportResult]:
    """기존 CSV는 유지하고 선택한 run의 accepted 콘텐츠만 중복 없이 누적한다.

    ponytail: export_type()처럼 DB의 content_hash 컬럼을 바로 쓰지 못하고 CSV를 다시 읽어
    title+content로 재계산한다 — "이미 CSV에 실린 것"을 DB가 아니라 CSV 파일 자체로만 알 수
    있기 때문이다(다른 run이 먼저 accepted시킨 같은 type의 콘텐츠를 이 run 시점엔 아직 CSV에
    넣으면 안 되므로, export_type()처럼 "그 type의 전체 accepted"를 그냥 다시 쓸 수는 없다 —
    2026-08-27, export_type() 재사용을 시도했다가 run 간 격리 테스트가 깨져서 확인함).
    CSV export 시점에 content_hash를 같이 저장하는 사이드 인덱스를 두면 이 재계산을 없앨 수
    있지만, 스키마 변경이 필요한 별도 작업이라 지금은 하지 않는다.
    """
    pairs = conn.execute(
        """
        SELECT DISTINCT q.taxonomy_lv2, q.type_name
        FROM content_discoveries cd
        JOIN search_queries q ON q.id = cd.query_id
        JOIN contents c ON c.id = cd.content_id
        JOIN content_taxonomy_mappings m
          ON m.content_id = c.id
         AND m.taxonomy_lv2 = q.taxonomy_lv2
         AND m.type_name = q.type_name
        WHERE cd.run_id = ? AND m.decision = 'accepted' AND c.status = 'accepted'
        """,
        (run_id,),
    ).fetchall()

    results = []
    for pair in pairs:
        lv2_id, type_name = pair["taxonomy_lv2"], pair["type_name"]
        path = final_dir / lv2_id / f"{type_name}.csv"
        existing_rows: list[dict[str, str]] = []
        if path.exists():
            with open(path, newline="", encoding="utf-8-sig") as f:
                existing_rows = list(csv.DictReader(f))

        rows = existing_rows + [
            _csv_row(row) for row in _accepted_rows_by_run(conn, run_id, lv2_id, type_name)
        ]
        unique_rows = []
        seen_urls: set[str] = set()
        seen_contents: set[str] = set()
        for row in rows:
            normalized_url = normalize_url(row["url"])
            content_hash = compute_content_hash(row["title"], row["content"])
            if normalized_url in seen_urls or content_hash in seen_contents:
                continue
            seen_urls.add(normalized_url)
            seen_contents.add(content_hash)
            unique_rows.append(row)

        _write_csv(path, unique_rows)
        results.append(ExportResult(lv2_id, type_name, path, len(unique_rows)))
    return results
