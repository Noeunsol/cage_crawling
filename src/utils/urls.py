"""URL 정규화: 추적 파라미터·fragment 제거, query parameter 정렬.

모바일/데스크톱 URL 통합처럼 사이트마다 다른 규칙은 여기서 다루지 않는다.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "igshid", "ref", "referrer",
}

# 일부 사이트가 href를 JS로 조립하면서 "=" 등을 \uXXXX로 이스케이프해놓고 실제로 디코딩을
# 안 해서, Tavily/SerpAPI가 그 raw 문자열("...idxno=333363")을 그대로 URL로 돌려주는
# 경우가 있다 — 그러면 진짜 물음표 파라미터가 아니라 리터럴 "=" 텍스트가 붙어서 404/빈
# 페이지로 이어지고 extraction_failed로 낭비된다(2026-08-26 실측: mediatoday.co.kr 11건).
_LEAKED_JS_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_ENCODED_JS_UNICODE_ESCAPE = re.compile(r"%5cu([0-9a-fA-F]{4})", re.IGNORECASE)


def normalize_url(url: str) -> str:
    url = url.strip()
    # \uXXXX 복구는 쿼리 문자열(? 뒤)에만 적용한다 — scheme/host/path에 우연히 같은 패턴이
    # 있는 다른 사이트의 정상 URL까지 건드려 엉뚱한 리소스로 바꿔치기하는 걸 막기 위해서다.
    # 실제 버그(mediatoday.co.kr)는 항상 쿼리 파라미터 값 안에서만 나타났다.
    before_query, sep, query_and_fragment = url.partition("?")
    if sep:
        # 이미 URL 인코딩된 역슬래시(%5C)도 먼저 복구한다. 과거 mediatoday URL처럼
        # 잘못된 key가 urlencode를 한 번 거치며 끝에 붙은 빈 값의 '='도 함께 걷어낸다.
        query_and_fragment = re.sub(
            r"%5cu003d([^&#=]+)=?(?=&|#|$)", r"=\1", query_and_fragment, flags=re.IGNORECASE,
        )
        query_and_fragment = _ENCODED_JS_UNICODE_ESCAPE.sub(
            lambda m: chr(int(m.group(1), 16)), query_and_fragment,
        )
        query_and_fragment = _LEAKED_JS_UNICODE_ESCAPE.sub(
            lambda m: chr(int(m.group(1), 16)), query_and_fragment,
        )
        url = before_query + sep + query_and_fragment
    parts = urlsplit(url)
    query_pairs = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((
        parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query_pairs), "",  # fragment 제거
    ))


def is_blocklisted_domain(domain: str, blacklist_domains: list[str]) -> bool:
    """루트 도메인을 등록하면 ``www``·모바일 등 모든 하위 도메인도 차단한다."""
    domain = domain.lower().split(":", 1)[0].rstrip(".")
    return any(domain == blocked or domain.endswith(f".{blocked}") for blocked in blacklist_domains)
