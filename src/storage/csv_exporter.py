"""DB의 accepted 콘텐츠를 LV2/type별 CSV로 다시 만든다 (13절).

CSV에 직접 append하지 않는다 — export할 때마다 DB 기준으로 파일 전체를 새로 쓴다 (13.2절).
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

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


def export_type(conn: sqlite3.Connection, lv2_id: str, type_name: str, final_dir: Path) -> ExportResult:
    """이 type CSV 하나를 통째로 다시 쓴다. 같은 content_hash는 한 번만 넣는다 (13.2절)."""
    seen_hashes: set[str] = set()
    unique_rows = []
    for row in _accepted_rows(conn, lv2_id, type_name):
        if row["content_hash"] in seen_hashes:
            continue
        seen_hashes.add(row["content_hash"])
        unique_rows.append(row)

    out_dir = final_dir / lv2_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{type_name}.csv"

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in unique_rows:
            writer.writerow({
                "title": row["title"],
                "content": row["content"],
                "date": row["published_date"] or "",
                "url": row["canonical_url"],
                "source_domain": row["source_domain"],
            })

    return ExportResult(lv2_id=lv2_id, type_name=type_name, path=path, row_count=len(unique_rows))


def export_all(
    conn: sqlite3.Connection, final_dir: Path, targets: list[tuple[str, str]] | None = None,
) -> list[ExportResult]:
    """targets를 안 주면 accepted 콘텐츠가 있는 모든 (LV2, type)을 대상으로 한다."""
    targets = accepted_type_pairs(conn) if targets is None else targets
    return [export_type(conn, lv2_id, type_name, final_dir) for lv2_id, type_name in targets]
