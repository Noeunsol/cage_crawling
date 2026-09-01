"""이전 run에서 retryable=1로 discard된 후보를 새 검색 없이 다시 fetch 큐에 올린다.

discarded_candidates.retryable는 이미 retry_policy.yaml 기준으로 정확히 채워지고 있었지만, 실제로
재시도하는 코드가 없었다(discarded_repo.list_retryable()이 테스트에서만 호출되는 죽은 함수였다).
여기서 실제로 연결한다 — 새 검색 API 호출 없이, 이미 알고 있는 URL의 fetch만 다시 시도한다.

재시도 대상은 retry_policy.yaml의 reasons[reason].retry_mode=="immediate"인 것들(timeout,
temporary_http_error, extraction_too_short, unexpected_error)이다. extraction_empty(구조적으로
파싱 불가능해 파서를 고쳐야 하는 경우)는 retry_mode="after_parser_update"라 자동 재시도 대상이
아니고, access_denied/not_found/blacklisted_domain/duplicate/date_out_of_range/
low_korea_relevance/taxonomy_mismatch는 retry_mode="never"라 애초에 제외된다
(src/utils/retry_policy_helpers.py가 discarded_candidates.retryable을 이 기준으로 계산해 저장한다).
"""

from __future__ import annotations

import sqlite3

from src.discovery.scheduler import ScheduledCandidate
from src.storage.repositories import contents as contents_repo


def find_retryable_candidates(
    conn: sqlite3.Connection, configs: dict, targets: list[tuple[str, str]],
) -> list[ScheduledCandidate]:
    """(lv2, type)마다 재시도 대상 URL을 모은다. max_attempts를 넘겼거나 이미 저장된 건 제외."""
    reasons_cfg = configs["retry_policy"]["reasons"]
    candidates: list[ScheduledCandidate] = []

    for lv2_id, type_name in targets:
        rows = conn.execute(
            """
            SELECT dc.original_url, dc.normalized_url, dc.reason, dc.query_id, sq.provider,
                   (SELECT COUNT(*) FROM discarded_candidates dc2
                    WHERE dc2.normalized_url = dc.normalized_url) AS attempt_count
            FROM discarded_candidates dc
            JOIN search_queries sq ON sq.id = dc.query_id
            WHERE sq.taxonomy_lv2 = ? AND sq.type_name = ? AND dc.retryable = 1
            ORDER BY dc.discarded_at DESC
            """,
            (lv2_id, type_name),
        ).fetchall()

        seen_urls: set[str] = set()
        for row in rows:
            url = row["normalized_url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            if contents_repo.get_by_canonical_url(conn, url) is not None:
                continue  # 이미 다른 경로로 성공 저장됨 — 재시도 불필요

            max_attempts = reasons_cfg.get(row["reason"], {}).get("max_attempts", 1)
            if row["attempt_count"] >= max_attempts:
                continue

            candidates.append(ScheduledCandidate(
                lv2_id=lv2_id, type_name=type_name, provider=row["provider"],
                query_id=row["query_id"], url=row["original_url"], rank=0, relevance_score=None,
            ))

    return candidates
