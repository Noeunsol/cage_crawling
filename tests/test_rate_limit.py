import time

from src.utils.rate_limit import _last_call_at, throttle


def test_throttle_waits_for_min_interval():
    _last_call_at.pop("test_key", None)
    throttle("test_key", 0.2)  # 첫 호출은 대기 없음
    started = time.monotonic()
    throttle("test_key", 0.2)  # 바로 다시 부르면 남은 간격만큼 대기
    elapsed = time.monotonic() - started
    assert elapsed >= 0.15  # 타이밍 오차 감안


def test_throttle_skips_wait_when_interval_already_passed():
    _last_call_at.pop("test_key2", None)
    throttle("test_key2", 0.05)
    time.sleep(0.1)
    started = time.monotonic()
    throttle("test_key2", 0.05)
    elapsed = time.monotonic() - started
    assert elapsed < 0.05


def test_throttle_noop_when_min_interval_zero():
    _last_call_at.pop("test_key3", None)
    started = time.monotonic()
    throttle("test_key3", 0)
    throttle("test_key3", 0)
    assert time.monotonic() - started < 0.05
