"""2차 수집용 coverage 분석. 1차 결과에서 부족한 taxonomy_lv2를 찾는다.

effective = accepted + review×review_weight
deficit   = target − effective   (deficit>0 인 LV2만 2차 대상)
"""
from __future__ import annotations

import sqlite3


def weighted_coverage_by_lv2(conn: sqlite3.Connection, review_weight: float = 0.5) -> dict[str, float]:
    """LV2별 유효 커버리지. accepted 1.0 + review×w. supplementary/미매핑 제외."""
    rows = conn.execute(
        """SELECT taxonomy_lv2, action, COUNT(*) FROM content_records
           WHERE action IN ('accepted','review') AND COALESCE(is_supplementary,0)=0
             AND taxonomy_lv2 IS NOT NULL AND taxonomy_lv2!=''
           GROUP BY taxonomy_lv2, action"""
    ).fetchall()
    out: dict[str, float] = {}
    for lv2, action, count in rows:
        out[lv2] = out.get(lv2, 0.0) + count * (1.0 if action == "accepted" else review_weight)
    return out


def rank_deficits(
    coverage: dict[str, float],
    targets: dict[str, float],
    exclude_sufficient: bool = True,
) -> list[dict]:
    """목표 대비 부족분을 deficit 내림차순으로 정렬. exclude_sufficient면 deficit>0만."""
    ranked = []
    for lv2, target in targets.items():
        effective = round(coverage.get(lv2, 0.0), 3)
        deficit = round(target - effective, 3)
        if exclude_sufficient and deficit <= 0:
            continue
        ranked.append({"lv2": lv2, "target": target, "effective": effective, "deficit": deficit})
    ranked.sort(key=lambda r: r["deficit"], reverse=True)
    return ranked


def resolve_targets(policies, target_selection: dict) -> dict[str, float]:
    """enabled taxonomy_lv2별 목표치. targets_by_lv2 override, 없으면 min_accepted_per_lv2."""
    default = float(target_selection.get("min_accepted_per_lv2", 30))
    override = target_selection.get("targets_by_lv2", {}) or {}
    return {p.taxonomy_lv2: float(override.get(p.taxonomy_lv2, default)) for p in policies}


if __name__ == "__main__":
    cov = {"4_I": 7.0, "1_A": 30.0}          # 1_A는 충분, 4_I는 부족
    targets = {"4_I": 30, "1_A": 30, "6_Q": 20}
    ranked = rank_deficits(cov, targets)
    assert [r["lv2"] for r in ranked] == ["4_I", "6_Q"], ranked   # deficit 23 > 20, 1_A 제외
    assert ranked[0]["deficit"] == 23.0, ranked
    # in-memory sqlite로 weighted 합산 검증
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE content_records (taxonomy_lv2 TEXT, action TEXT, is_supplementary INT)")
    conn.executemany(
        "INSERT INTO content_records VALUES (?,?,?)",
        [("4_I", "accepted", 0), ("4_I", "review", 0), ("4_I", "review", 0), ("4_I", "excluded", 0)],
    )
    cov2 = weighted_coverage_by_lv2(conn, 0.5)
    assert cov2 == {"4_I": 2.0}, cov2          # 1 accepted + 2×0.5 review, excluded 무시
    print("coverage self-check OK")
