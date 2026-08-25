"""요청 간 최소 간격을 강제하는 스로틀러 (retry_policy.yaml의 rate_limit 설정을 실제로 적용한다).

이 프로젝트는 요청을 순차적으로만 보내므로(동시 요청 없음) max_concurrency는 항상 만족돼 있다 —
그래서 여기서는 min_interval_seconds만 강제한다.
"""

from __future__ import annotations

import time

_last_call_at: dict[str, float] = {}


def throttle(key: str, min_interval_seconds: float) -> None:
    """key(provider 이름)로 마지막 호출 이후 min_interval_seconds가 안 지났으면 그만큼 sleep한다."""
    if min_interval_seconds <= 0:
        return
    now = time.monotonic()
    last = _last_call_at.get(key)
    if last is not None:
        remaining = min_interval_seconds - (now - last)
        if remaining > 0:
            time.sleep(remaining)
    _last_call_at[key] = time.monotonic()
