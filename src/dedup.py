"""단계9 — 동일 사건(near-dup) 중복 관리. URL-dedup과 별도.

SimHash(64bit)로 유사 본문을 잡고, event_key(정규화 title+date+site)를 보조 규칙으로 둔다.
한계: SimHash는 유사 '문장' 기반이라 같은 사건을 완전히 다른 문장으로 재작성하면 놓칠 수
있음. 추후 embedding 기반 dedup으로 확장 (ponytail: 지금은 재게시·언론 받아쓰기 중복만 목표).
"""
from __future__ import annotations

import hashlib
import re

_TOKEN = re.compile(r"[가-힣a-zA-Z0-9]+")
_MASK64 = (1 << 64) - 1


def simhash(text: str) -> int:
    """토큰 2-gram shingle 기반 64bit SimHash."""
    tokens = _TOKEN.findall(text or "")
    shingles = tokens if len(tokens) < 2 else [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
    if not shingles:
        return 0
    v = [0] * 64
    for sh in shingles:
        h = int(hashlib.blake2b(sh.encode("utf-8"), digest_size=8).hexdigest(), 16)
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= (1 << i)
    return out & _MASK64


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & _MASK64).count("1")


def make_event_key(title: str, published_at: str | None, site_name: str) -> str:
    """보조 사건 키: 정규화 title + 날짜(YYYY-MM-DD) + site."""
    norm = "".join(_TOKEN.findall((title or "").lower()))
    date = (published_at or "")[:10]
    return f"{site_name}|{date}|{norm}"


class EventDeduper:
    """같은 subtype 안에서 near-dup을 찾는다. 선형 스캔(ponytail: 규모 커지면 상위비트 버킷팅)."""

    def __init__(self, hamming_threshold: int = 3):
        self.threshold = hamming_threshold
        self._seen: list[tuple[str, int, str]] = []   # (subtype, simhash, source_url)
        self._event_keys: dict[tuple[str, str], str] = {}  # (subtype,event_key) → source_url

    def check(self, subtype: str, sh: int, event_key: str) -> str | None:
        """중복이면 원본 source_url 반환, 아니면 None."""
        key = (subtype, event_key)
        if event_key and key in self._event_keys:
            return self._event_keys[key]
        for st, other, url in self._seen:
            if st == subtype and hamming(sh, other) <= self.threshold:
                return url
        return None

    def add(self, subtype: str, sh: int, event_key: str, source_url: str) -> None:
        self._seen.append((subtype, sh, source_url))
        if event_key:
            self._event_keys.setdefault((subtype, event_key), source_url)
