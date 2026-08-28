"""LLM이 만든 짧은 표현(vocabulary 후보 등)에서 구조적으로 위험한 항목을 거르는 공용 필터.

프롬프트로 "짧은 명사구만, 방법·도구·신체부위·치명성·개인식별 정보 금지"를 지시해도 LLM이 항상
지키지는 않으므로(다른 규칙들도 코드로 이중 강제하는 것과 동일한 이유) 구조적으로 한 번 더 거른다.
실제 위험 키워드를 나열해 차단하는 방식은 그 목록 자체가 민감 정보라 쓰지 않고, 대신 "짧은 명사구가
아니다"를 가리키는 구조적 신호(길이, 숫자, 연락처/URL 패턴)만 본다. LLM이 생성한 짧은 표현 목록을
다루는 곳(현재는 src/query/freshness.py)이라면 어디서든 재사용할 수 있다.
"""

from __future__ import annotations

import re

_MAX_TERM_CHARS = 12
_UNSAFE_TERM_PATTERN = re.compile(r"\d|@|https?://|www\.")


def sanitize_short_terms(terms: list[str]) -> list[str]:
    return [t for t in terms if t and len(t) <= _MAX_TERM_CHARS and not _UNSAFE_TERM_PATTERN.search(t)]
