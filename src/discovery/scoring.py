"""쿼리/도메인 진단 점수 — 삭제·차단이 아니라 "다음 실행에서 순서"만 바꾼다.

반복적으로 나쁜 신호(0건 응답, 추출 실패만 남는 도메인)를 낸 쿼리/도메인은 점수가 낮아져 lane
큐/번들 선택에서 뒤로 밀린다(여전히 선택은 됨 — blacklist.yaml처럼 아예 빼는 것과 다르다).
시간 감쇠를 적용해 예전 한 번의 실패가 계속 발목잡지 않게 한다(최근 결과에 더 큰 가중치).
표본이 없으면 중립값 0.5를 돌려줘서 신규 쿼리/도메인이 부당하게 밀리지 않게 한다.
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timezone

_HALF_LIFE_DAYS = 14  # 이 기간이 지나면 과거 결과의 가중치가 절반으로 줄어든다
_NEUTRAL_SCORE = 0.5
DEFAULT_EXPLORATION_EPSILON = 0.2  # 점수만 따르면 한번 낮은 점수가 붙은 쿼리/도메인은 표본을
                                    # 다시 못 쌓아 영원히 밀린다(starvation) — 20%는 무작위로 섞는다.


def _decay_weight(timestamp_str: str | None, now: datetime) -> float:
    if not timestamp_str:
        return 0.0
    ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    days_ago = max((now - ts).total_seconds() / 86400, 0)
    return 0.5 ** (days_ago / _HALF_LIFE_DAYS)


def query_score(conn: sqlite3.Connection, query_id: int, *, now: datetime | None = None) -> float:
    """0~1. 높을수록 이 쿼리를 이번 run에서 먼저 시도할 가치가 있다는 뜻."""
    now = now or datetime.now(timezone.utc)
    executions = conn.execute(
        "SELECT result_count, started_at FROM query_executions WHERE query_id = ? AND status = 'success'",
        (query_id,),
    ).fetchall()
    candidates = conn.execute(
        """
        SELECT c.status, d.discovered_at FROM content_discoveries d
        JOIN contents c ON c.id = d.content_id
        WHERE d.query_id = ?
        """,
        (query_id,),
    ).fetchall()
    if not executions and not candidates:
        return _NEUTRAL_SCORE

    weighted_sum, weight_total = 0.0, 0.0
    for row in executions:
        w = _decay_weight(row["started_at"], now)
        weight_total += w
        weighted_sum += w * (1.0 if (row["result_count"] or 0) > 0 else 0.0)
    for row in candidates:
        w = _decay_weight(row["discovered_at"], now)
        weight_total += w
        # accepted면 만점, 아니면(중복/기간밖/한국관련성 등으로 excluded) 0건보다는 낫다는 의미로 부분 점수.
        weighted_sum += w * (1.0 if row["status"] == "accepted" else 0.3)

    return weighted_sum / weight_total if weight_total else _NEUTRAL_SCORE


def sort_queries_by_score(
    conn: sqlite3.Connection, queries: list[sqlite3.Row],
    *, epsilon: float = DEFAULT_EXPLORATION_EPSILON, rng: random.Random | None = None,
) -> list[sqlite3.Row]:
    """active_queries를 lane.queries에 넣을 순서로 정렬한다.

    기본은 점수 내림차순(활용)이지만, 매 슬롯마다 epsilon 확률로 무작위 하나를 대신 뽑는다
    (탐색) — 그래야 낮은 점수가 붙은 쿼리도 가끔 다시 시도돼서 표본이 갱신될 기회가 생긴다.
    표본이 전혀 없어 전부 중립값(동점)이면 탐색이고 뭐고 의미가 없으니 원래 순서를 그대로 쓴다
    (기존 테스트가 기대하는 결정적 순서와도 호환된다).
    """
    if len(queries) <= 1:
        return list(queries)

    now = datetime.now(timezone.utc)
    scored = [(row, query_score(conn, row["id"], now=now)) for row in queries]
    if len({round(score, 6) for _, score in scored}) == 1:
        return [row for row, _ in scored]

    rng = rng or random
    pool = list(scored)
    ordered: list[sqlite3.Row] = []
    while pool:
        if len(pool) > 1 and rng.random() < epsilon:
            idx = rng.randrange(len(pool))
        else:
            idx = max(range(len(pool)), key=lambda i: pool[i][1])
        row, _ = pool.pop(idx)
        ordered.append(row)
    return ordered


_EXTRACTION_FAILURE_REASONS = ("extraction_empty", "extraction_too_short")


def domain_score(conn: sqlite3.Connection, domain: str, *, now: datetime | None = None) -> float:
    """0~1. 본문 추출 성공 이력(시간감쇠) - 연속 실패 패널티."""
    now = now or datetime.now(timezone.utc)
    placeholders = ",".join("?" * len(_EXTRACTION_FAILURE_REASONS))
    success_rows = conn.execute(
        "SELECT first_discovered_at FROM contents WHERE source_domain = ?", (domain,)
    ).fetchall()
    failed_rows = conn.execute(
        f"SELECT discarded_at FROM discarded_candidates WHERE source_domain = ? AND reason IN ({placeholders})",
        (domain, *_EXTRACTION_FAILURE_REASONS),
    ).fetchall()
    if not success_rows and not failed_rows:
        return _NEUTRAL_SCORE

    weighted_sum, weight_total = 0.0, 0.0
    for row in success_rows:
        w = _decay_weight(row["first_discovered_at"], now)
        weight_total += w
        weighted_sum += w
    for row in failed_rows:
        weight_total += _decay_weight(row["discarded_at"], now)
    base_score = weighted_sum / weight_total if weight_total else _NEUTRAL_SCORE

    recent = conn.execute(
        f"""
        SELECT kind, at FROM (
            SELECT 'success' AS kind, first_discovered_at AS at FROM contents WHERE source_domain = ?
            UNION ALL
            SELECT 'fail' AS kind, discarded_at AS at FROM discarded_candidates
            WHERE source_domain = ? AND reason IN ({placeholders})
        )
        ORDER BY at DESC LIMIT 5
        """,
        (domain, domain, *_EXTRACTION_FAILURE_REASONS),
    ).fetchall()
    consecutive_failures = 0
    for row in recent:
        if row["kind"] != "fail":
            break
        consecutive_failures += 1
    penalty = min(consecutive_failures * 0.1, 0.4)

    return max(base_score - penalty, 0.0)


def pick_scored_bundle(
    conn: sqlite3.Connection, bundles_repo_module, type_name: str,
    *, epsilon: float = DEFAULT_EXPLORATION_EPSILON, rng: random.Random | None = None,
):
    """LRU 대신 도메인 점수를 우선(동점이면 LRU)으로 활성 번들 하나를 고른다.

    epsilon 확률로 점수와 무관하게 무작위 활성 번들을 고른다(탐색) — 점수 낮은 번들이 영영
    안 뽑혀서 최신 성과를 다시 잴 기회조차 없어지는 걸 막는다. 반환값은 bundles_repo.DomainBundle과
    동일한 모양 — pick_bundle()을 그대로 대체해 쓴다.
    """
    rows = conn.execute(
        "SELECT bundle_index, domains, last_used_at FROM serpapi_domain_bundles "
        "WHERE type_name = ? AND enabled = 1",
        (type_name,),
    ).fetchall()
    if not rows:
        return None

    now = datetime.now(timezone.utc)

    def sort_key(row: sqlite3.Row) -> tuple:
        domains = json.loads(row["domains"])
        avg_score = sum(domain_score(conn, d, now=now) for d in domains) / len(domains)
        never_used = row["last_used_at"] is None
        # 점수 내림차순 우선, 그다음 한 번도 안 쓴 걸 우선, 그다음 오래 전에 쓴 순(LRU).
        return (-avg_score, 0 if never_used else 1, row["last_used_at"] or "")

    scores = [sort_key(row)[0] for row in rows]  # -avg_score만 비교 — 전부 동점이면 표본이 없다는 뜻
    rng = rng or random
    if len(rows) > 1 and len(set(round(s, 6) for s in scores)) > 1 and rng.random() < epsilon:
        best = rng.choice(rows)
    else:
        best = min(rows, key=sort_key)

    return bundles_repo_module.DomainBundle(
        type_name=type_name, bundle_index=best["bundle_index"], domains=json.loads(best["domains"]),
    )
