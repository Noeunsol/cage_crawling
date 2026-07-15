"""Phase 12 — Dataset Report. DB를 집계해 coverage/소스분포/필터로그/품질 지표 산출."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .store import Store


def build_report(store: Store) -> dict:
    cur = store.conn.cursor()

    def counts(sql: str) -> dict:
        return {str(k): v for k, v in cur.execute(sql).fetchall()}

    total = cur.execute("SELECT COUNT(*) FROM content_records").fetchone()[0]
    filter_fail = counts(
        "SELECT reason, COUNT(*) FROM filter_logs WHERE status='fail' GROUP BY reason"
    )
    missing_date = cur.execute(
        "SELECT COUNT(*) FROM content_records WHERE published_at IS NULL OR published_at=''"
    ).fetchone()[0]
    avg_quality = cur.execute(
        "SELECT AVG(quality_score) FROM content_records"
    ).fetchone()[0]
    avg_conf = cur.execute(
        "SELECT AVG(taxonomy_relevance_score) FROM content_records"
    ).fetchone()[0]

    return {
        "stored_records": total,
        "by_taxonomy": counts("SELECT taxonomy_lv2, COUNT(*) FROM content_records GROUP BY taxonomy_lv2"),
        "by_subtype": counts("SELECT subtype, COUNT(*) FROM content_records GROUP BY subtype"),
        "by_site": counts("SELECT site_name, COUNT(*) FROM content_records GROUP BY site_name"),
        "by_search_api": counts("SELECT search_api, COUNT(*) FROM content_records GROUP BY search_api"),
        "by_collection_method": counts("SELECT collection_method, COUNT(*) FROM content_records GROUP BY collection_method"),
        "by_extractor": counts("SELECT extractor, COUNT(*) FROM content_records GROUP BY extractor"),
        "by_filter_status": counts("SELECT filter_status, COUNT(*) FROM content_records GROUP BY filter_status"),
        "filter_fail_reasons": filter_fail,
        "missing_published_at_ratio": round(missing_date / total, 3) if total else 0.0,
        "average_quality_score": round(avg_quality, 3) if avg_quality else 0.0,
        "average_taxonomy_confidence": round(avg_conf, 3) if avg_conf else 0.0,
    }


def print_report(report: dict) -> None:
    print("\n===== Dataset Report =====")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def export_report(report: dict, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def export_csv(store: Store, path: str) -> int:
    """content_records 전체를 CSV로 내보낸다. 반환: 행 수. (Excel 호환 utf-8-sig)"""
    cur = store.conn.execute("SELECT * FROM content_records")
    columns = [d[0] for d in cur.description]
    rows = cur.fetchall()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        w.writerows(rows)
    return len(rows)
