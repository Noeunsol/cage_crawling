"""본문 텍스트 정규화와 콘텐츠 해시 (9.4, 10.3절)."""

from __future__ import annotations

import hashlib
import re


def normalize_whitespace(text: str) -> str:
    """문단 개행은 유지하면서 연속 공백/빈 줄만 정리한다 (9.4절)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def compute_content_hash(title: str, content: str) -> str:
    """제목+본문이 완전히 같은 콘텐츠를 다른 URL로 발견했을 때 판단하는 기준 (10.3절).

    유사도 기반 event-level dedup(같은 사건을 다르게 쓴 기사 등)은 별도 설정으로 분리될
    영역이라 여기서는 다루지 않고, 정확히 같은 텍스트인지만 본다.
    """
    canonical = normalize_whitespace(f"{title}\n{content}").lower()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
