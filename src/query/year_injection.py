"""날짜 표현 대신 연도만 조건부로 검색어에 추가한다 (7절 확장, 2026-08-31).

기본 원칙은 그대로다 — 상대적 날짜 표현(최근/지난달/올해 등)은 검색어에 넣지 않고 API 날짜
필터가 담당한다(src/query/generator.py의 _DATE_EXPRESSION). 다만 tavily topic=general처럼
날짜 필터가 자꾸 새는 (lv2, type, provider)는 검색 결과 자체를 최신 연도로 편향시켜 보조한다.
날짜 필터의 대체가 아니라 결과 편향을 줄이는 보조 수단이므로, 여기서 만든 변형 쿼리도 API 날짜
필터/추출 후 날짜 필터를 그대로 통과해야 accepted가 된다.
"""

from __future__ import annotations

import sqlite3
from datetime import date


def _date_out_of_range_rate(conn: sqlite3.Connection, lv2_id: str, type_name: str, provider: str) -> tuple[float | None, int]:
    """(rate, total). total 표본이 0이면 rate는 None."""
    row = conn.execute(
        """
        SELECT COUNT(*) total,
               SUM(CASE WHEN m.decision_reason LIKE 'date_out_of_range%' THEN 1 ELSE 0 END) date_out
        FROM content_discoveries d
        JOIN search_queries sq ON sq.id = d.query_id
        JOIN content_taxonomy_mappings m
            ON m.content_id = d.content_id AND m.taxonomy_lv2 = sq.taxonomy_lv2 AND m.type_name = sq.type_name
        WHERE sq.taxonomy_lv2 = ? AND sq.type_name = ? AND sq.provider = ?
        """,
        (lv2_id, type_name, provider),
    ).fetchone()
    total = row["total"] or 0
    if total == 0:
        return None, 0
    return (row["date_out"] or 0) / total, total


def should_inject_year(conn: sqlite3.Connection, configs: dict, lv2_id: str, type_name: str, provider: str) -> bool:
    policy = configs["collection"].get("date_query_policy", {})
    if not policy.get("allow_year", False):
        return False
    year_cfg = policy.get("year_injection", {})
    rate, total = _date_out_of_range_rate(conn, lv2_id, type_name, provider)
    if rate is None or total < year_cfg.get("min_candidates", 20):
        return False
    return rate >= year_cfg.get("date_out_of_range_threshold", 0.30)


def years_in_range(date_from: date, date_to: date) -> list[int]:
    return list(range(date_from.year, date_to.year + 1))


def build_year_variants(query_text: str, date_from: date, date_to: date) -> list[str]:
    """query_text에 연도를 붙인 변형들을 만든다 — 기간이 여러 해에 걸치면 해마다 하나씩."""
    return [f"{query_text} {year}년" for year in years_in_range(date_from, date_to)]
