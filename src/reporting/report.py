"""Phase 12 — Dataset Report. DB를 집계해 coverage/소스분포/필터로그/품질 지표 산출."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from ..storage.store import Store


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
    by_action = {
        "accepted": cur.execute(
            "SELECT COUNT(*) FROM content_records WHERE action='accepted' AND COALESCE(is_supplementary,0)=0"
        ).fetchone()[0],
        "discard": cur.execute(
            "SELECT COUNT(*) FROM url_candidates "
            "WHERE status IN ('prefilter_discarded','trend_discard','matched_fail')"
        ).fetchone()[0],
        "supplementary": cur.execute(
            "SELECT COUNT(*) FROM content_records WHERE is_supplementary=1"
        ).fetchone()[0],
    }

    def average(column: str) -> float:
        value = cur.execute(f"SELECT AVG({column}) FROM content_records").fetchone()[0]
        return round(value or 0.0, 3)

    def conversion(group: str) -> dict:
        rows = cur.execute(f"""
            SELECT COALESCE({group},'unknown'), COUNT(*),
              SUM(CASE WHEN status IN ('extracted','quality_failed','matched_pass','matched_fail','duplicate') THEN 1 ELSE 0 END),
              SUM(CASE WHEN status='matched_pass' THEN 1 ELSE 0 END)
            FROM url_candidates GROUP BY {group}
        """).fetchall()
        return {str(k): {"discovered": d, "extracted": e or 0, "accepted": p or 0,
                         "conversion_rate": round((p or 0) / d, 3) if d else 0.0}
                for k, d, e, p in rows}

    all_rungs_failed = cur.execute(
        "SELECT COUNT(*) FROM filter_logs WHERE stage='extract' AND reason LIKE 'all_extractors_failed%'"
    ).fetchone()[0]
    dup_events = cur.execute(
        "SELECT COUNT(*) FROM filter_logs WHERE stage='event_dedup'"
    ).fetchone()[0]

    return {
        "stored_records": total,
        "by_taxonomy": counts("SELECT taxonomy_lv2, COUNT(*) FROM content_records GROUP BY taxonomy_lv2"),
        "by_taxonomy_accepted": counts(
            "SELECT taxonomy_lv2, COUNT(*) FROM content_records "
            "WHERE action='accepted' AND COALESCE(is_supplementary,0)=0 AND taxonomy_lv2 IS NOT NULL "
            "GROUP BY taxonomy_lv2"),
        "by_subtype": counts("SELECT subtype, COUNT(*) FROM content_records GROUP BY subtype"),
        "by_site": counts("SELECT site_name, COUNT(*) FROM content_records GROUP BY site_name"),
        "by_search_api": counts("SELECT search_api, COUNT(*) FROM content_records GROUP BY search_api"),
        "by_collection_type": counts("SELECT collection_type, COUNT(*) FROM content_records GROUP BY collection_type"),
        "by_discovery_method": counts("SELECT discovery_method, COUNT(*) FROM content_records GROUP BY discovery_method"),
        "by_extractor": counts("SELECT extractor, COUNT(*) FROM content_records GROUP BY extractor"),
        "by_filter_status": counts("SELECT filter_status, COUNT(*) FROM content_records GROUP BY filter_status"),
        # 트렌드 모드 집계 (keyword 모드에선 대부분 빈 버킷)
        "by_source": counts("SELECT source, COUNT(*) FROM content_records WHERE source!='' GROUP BY source"),
        "by_action": by_action,
        "risk_score_distribution": counts(
            "SELECT risk_score, COUNT(*) FROM content_records WHERE risk_score IS NOT NULL GROUP BY risk_score"),
        "trend_score_distribution": counts(
            "SELECT trend_score, COUNT(*) FROM content_records WHERE trend_score IS NOT NULL GROUP BY trend_score"),
        "published_at_source_distribution": counts(
            "SELECT published_at_source, COUNT(*) FROM content_records GROUP BY published_at_source"),
        "filter_fail_reasons": filter_fail,
        "all_rungs_failed": all_rungs_failed,
        "duplicate_event_rate": round(dup_events / (total + dup_events), 3) if (total + dup_events) else 0.0,
        "missing_published_at_ratio": round(missing_date / total, 3) if total else 0.0,
        "average_quality_score": round(avg_quality, 3) if avg_quality else 0.0,
        "average_taxonomy_confidence": round(avg_conf, 3) if avg_conf else 0.0,
        "average_harmfulness_score": average("harmfulness_score"),
        "average_taxonomy_fit_score": average("taxonomy_fit_score"),
        "average_seed_source_value_score": average("seed_source_value_score"),
        "average_pii_risk_score": average("pii_risk_score"),
        "reference_only_ratio": _status_ratio(cur, "reference_only"),
        "db_duplicate_rate": _status_ratio(cur, "duplicate"),
        "collection_type_conversion": conversion("collection_type"),
        "api_conversion": conversion("search_api"),
    }


def _status_ratio(cur, status: str) -> float:
    total = cur.execute("SELECT COUNT(*) FROM url_candidates").fetchone()[0]
    count = cur.execute("SELECT COUNT(*) FROM url_candidates WHERE status=?", (status,)).fetchone()[0]
    return round(count / total, 3) if total else 0.0


def print_report(report: dict) -> None:
    print("\n===== Dataset Report =====")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def export_report(report: dict, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


# raw/cleaned는 PII를 포함할 수 있으므로 기본 export에서 제외한다.
_RAW_COLS = {"raw_text", "raw_comments", "cleaned_text"}


def export_csv(store: Store, path: str, include_raw: bool = False) -> int:
    """content_records를 CSV로 내보낸다. 기본 raw_text/raw_comments 제외 (미마스킹 원문 보호)."""
    all_cols = [row[1] for row in store.conn.execute("PRAGMA table_info(content_records)")]
    cols = all_cols if include_raw else [c for c in all_cols if c not in _RAW_COLS]
    cur = store.conn.execute(f"SELECT {','.join(cols)} FROM content_records")
    rows = cur.fetchall()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    return len(rows)
