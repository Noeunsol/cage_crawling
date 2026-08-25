"""URL 정규화 (10.2절): 추적 파라미터·fragment 제거, query parameter 정렬.

모바일/데스크톱 URL 통합처럼 사이트마다 다른 규칙은 여기서 다루지 않는다 (사이트별 TBD, 9.3절과 같은 원칙).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "igshid", "ref", "referrer",
}


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query_pairs = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((
        parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query_pairs), "",  # fragment 제거
    ))
