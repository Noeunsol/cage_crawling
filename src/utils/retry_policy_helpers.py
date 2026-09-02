"""retry_policy.yaml의 3단계 retry_mode를 읽는 헬퍼 (2026-08-31).

discarded_candidates.retryable(bool)은 retry_mode=="immediate"일 때만 True다.
after_parser_update/never는 둘 다 DB엔 retryable=False로 저장되지만, retry_mode 필드로만
구분된다 — "왜 자동 재시도가 안 됐는지"는 retry_policy.yaml을 사람이 조회해서 판단한다.
"""

from __future__ import annotations


def is_immediately_retryable(reasons_cfg: dict, reason: str) -> bool:
    return reasons_cfg.get(reason, {}).get("retry_mode") == "immediate"
