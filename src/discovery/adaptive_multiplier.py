"""provider별 실측 생존율 기반 candidate_multiplier.

tavily/serpapi는 필터 통과율이 구조적으로 다르다(예: tavily는 topic=general일 때 date_out_of_range로
많이 걸러짐). 하나의 candidate_multiplier를 두 provider에 똑같이 적용하면 생존율 낮은 쪽은
목표 미달, 높은 쪽은 과도하게 넉넉해진다 — (lv2, type, provider) 단위로 실측 생존율을 보고
provider마다 다른 배수를 쓴다.

계층형 폴백(2026-08-31 추가): (lv2,type,provider) 표본이 부족하면 바로 고정값으로 떨어지지 않고
더 넓은 단위(lv2,provider) → (provider 전체)로 한 단계씩 넓혀가며 표본을 찾는다 — type이 막
추가돼서 이력이 없어도, 같은 LV2의 다른 type이나 provider 전반의 경향을 우선 반영하는 게
아무 근거 없는 고정값보다 낫다. 그마저도 없으면 configs.collection.yaml의 initial 고정값.

DB에 누적된 전체 이력(단순 누적 평균 — EMA 상태를 따로 저장하지 않아도 되는 가장 단순한 형태)을
써서 자동 전환한다. run 한 번의 튐으로 급변하지 않도록 "최근 N건"이 아니라 "지금까지 전체"를 본다.
"""

from __future__ import annotations

import sqlite3


def _survival_stats(
    conn: sqlite3.Connection, provider: str, *, lv2_id: str | None = None, type_name: str | None = None,
) -> tuple[int, int]:
    """(accepted, total). lv2_id/type_name을 None으로 두면 그 조건 없이 더 넓게 집계한다.

    total = 검색이 실제로 찾아낸 후보 전체(fetch 성공/실패 무관), accepted = 그중 최종 accepted.
    """
    where = ["sq.provider = ?"]
    params: list = [provider]
    if lv2_id is not None:
        where.append("sq.taxonomy_lv2 = ?")
        params.append(lv2_id)
    if type_name is not None:
        where.append("sq.type_name = ?")
        params.append(type_name)
    where_sql = " AND ".join(where)

    discovered = conn.execute(
        f"""
        SELECT COUNT(*) total, SUM(CASE WHEN c.status = 'accepted' THEN 1 ELSE 0 END) accepted
        FROM content_discoveries d
        JOIN search_queries sq ON sq.id = d.query_id
        JOIN contents c ON c.id = d.content_id
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    discarded = conn.execute(
        f"""
        SELECT COUNT(*) n FROM discarded_candidates dc
        JOIN search_queries sq ON sq.id = dc.query_id
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    total = (discovered["total"] or 0) + (discarded["n"] or 0)
    accepted = discovered["accepted"] or 0
    return accepted, total


def _multiplier_from_rate(adaptive_cfg: dict, accepted: int, total: int) -> float:
    survival_rate = accepted / total
    multiplier = adaptive_cfg["safety_factor"] / max(survival_rate, adaptive_cfg["survival_rate_floor"])
    return min(max(multiplier, adaptive_cfg["min_value"]), adaptive_cfg["max_value"])


def compute_multiplier(conn: sqlite3.Connection, configs: dict, lv2_id: str, type_name: str, provider: str) -> float:
    provider_cfg = configs["collection"]["candidate_multiplier_by_provider"][provider]
    adaptive_cfg = configs["collection"]["adaptive_multiplier"]
    min_samples = adaptive_cfg["min_samples"]

    # 좁은 단위부터: (lv2,type,provider) -> (lv2,provider) -> (provider 전체) -> 고정 initial.
    tiers = [
        {"lv2_id": lv2_id, "type_name": type_name},
        {"lv2_id": lv2_id, "type_name": None},
        {"lv2_id": None, "type_name": None},
    ]
    for tier in tiers:
        accepted, total = _survival_stats(conn, provider, **tier)
        if total >= min_samples:
            return _multiplier_from_rate(adaptive_cfg, accepted, total)

    return provider_cfg["initial"]
