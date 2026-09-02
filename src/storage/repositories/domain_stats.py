"""도메인별 실제 본문 추출 성공/실패 이력 조회.

새 도메인(예: kin.naver.com)을 블랙리스트에 넣을지 type_domains에 추가할지는 여기 통계로
판단한다 — 검증 전에 임의로 막거나 열지 않는다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


# 2026-08-31: 예전엔 reason='extraction_failed' 하나였는데 extraction_empty/extraction_too_short로
# 쪼갰다 (retry_policy.yaml 참고) — 도메인 통계는 원인 구분 없이 "추출 자체가 실패했는지"만 보면
# 되므로 둘 다 합산한다.
_EXTRACTION_FAILURE_REASONS = ("extraction_empty", "extraction_too_short")


@dataclass
class DomainStats:
    domain: str
    success: int              # contents에 저장된 건수 (accepted+excluded 모두 — 추출 자체는 성공)
    extraction_failed: int    # discarded_candidates에서 추출 실패 사유(_EXTRACTION_FAILURE_REASONS)인 건수

    @property
    def total(self) -> int:
        return self.success + self.extraction_failed

    @property
    def success_rate(self) -> float | None:
        return self.success / self.total if self.total else None


def get_domain_stats(conn: sqlite3.Connection, domain: str) -> DomainStats:
    success = conn.execute(
        "SELECT COUNT(*) c FROM contents WHERE source_domain = ?", (domain,)
    ).fetchone()["c"]
    placeholders = ",".join("?" * len(_EXTRACTION_FAILURE_REASONS))
    failed = conn.execute(
        f"SELECT COUNT(*) c FROM discarded_candidates WHERE source_domain = ? AND reason IN ({placeholders})",
        (domain, *_EXTRACTION_FAILURE_REASONS),
    ).fetchone()["c"]
    return DomainStats(domain=domain, success=success, extraction_failed=failed)


def list_all_domain_stats(conn: sqlite3.Connection) -> list[DomainStats]:
    """지금까지 시도된 모든 도메인의 통계. 문제 도메인(성공률 낮음)을 한눈에 찾을 때 쓴다."""
    placeholders = ",".join("?" * len(_EXTRACTION_FAILURE_REASONS))
    success_rows = conn.execute(
        "SELECT source_domain AS domain, COUNT(*) c FROM contents GROUP BY source_domain"
    ).fetchall()
    failed_rows = conn.execute(
        f"SELECT source_domain AS domain, COUNT(*) c FROM discarded_candidates "
        f"WHERE reason IN ({placeholders}) GROUP BY source_domain",
        _EXTRACTION_FAILURE_REASONS,
    ).fetchall()
    success_by_domain = {r["domain"]: r["c"] for r in success_rows}
    failed_by_domain = {r["domain"]: r["c"] for r in failed_rows}
    domains = set(success_by_domain) | set(failed_by_domain)
    return [
        DomainStats(domain=d, success=success_by_domain.get(d, 0), extraction_failed=failed_by_domain.get(d, 0))
        for d in sorted(domains)
    ]
