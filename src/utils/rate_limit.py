"""요청 간 최소 간격을 강제하는 스로틀러 (retry_policy.yaml의 rate_limit 설정을 실제로 적용한다).

tavily/serpapi 검색은 여전히 순차 실행이라 min_interval_seconds만으로 충분하지만, fetch는
asyncio.to_thread로 동시 실행되므로(collector.py) 여러 스레드가 동시에 같은 key를 건드릴 수
있다 — check-and-set을 락으로 묶어 레이스로 min_interval이 무시되는 걸 막는다.
"""

from __future__ import annotations

import threading
import time

_last_call_at: dict[str, float] = {}
_lock = threading.Lock()


def throttle(key: str, min_interval_seconds: float) -> None:
    """key로 마지막 호출 이후 min_interval_seconds가 안 지났으면 그만큼 sleep한다."""
    if min_interval_seconds <= 0:
        return
    with _lock:
        now = time.monotonic()
        last = _last_call_at.get(key)
        remaining = (min_interval_seconds - (now - last)) if last is not None else 0
        _last_call_at[key] = now + max(remaining, 0)
    if remaining > 0:
        time.sleep(remaining)
