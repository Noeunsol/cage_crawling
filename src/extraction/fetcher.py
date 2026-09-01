"""원문 HTML을 가져온다. 실패 사유를 retry_policy.yaml의 reason code로 분류한다."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from src.utils.rate_limit import throttle
from src.utils.retry_policy_helpers import is_immediately_retryable


class FetchError(Exception):
    def __init__(self, reason: str, retryable: bool):
        self.reason = reason        # retry_policy.yaml의 reasons 키와 일치
        self.retryable = retryable
        super().__init__(reason)


@dataclass
class FetchResult:
    html: str
    final_url: str   # redirect를 따라간 최종 URL


def fetch(url: str, extraction_cfg: dict, retry_policy: dict) -> FetchResult:
    headers = {"User-Agent": extraction_cfg["fetch"]["user_agent"]}
    timeout = extraction_cfg["fetch"]["timeout_seconds"]

    # 도메인별로 스로틀링한다 — 전부 "fetch" 하나의 버킷으로 묶으면 서로 무관한 도메인끼리도
    # 불필요하게 직렬화된다 (동시 fetch를 도입한 이후엔 같은 도메인끼리만 예의를 지키면 된다).
    fetch_rate = retry_policy.get("rate_limit", {}).get("fetch", {})
    throttle(f"fetch:{urlsplit(url).netloc}", fetch_rate.get("min_interval_seconds", 0))

    reasons_cfg = retry_policy["reasons"]

    try:
        response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    except requests.exceptions.Timeout as e:
        raise FetchError("timeout", is_immediately_retryable(reasons_cfg, "timeout")) from e
    except requests.exceptions.RequestException as e:
        raise FetchError(
            "temporary_http_error", is_immediately_retryable(reasons_cfg, "temporary_http_error")
        ) from e

    status = response.status_code
    if status in (401, 403):
        raise FetchError("access_denied", is_immediately_retryable(reasons_cfg, "access_denied"))
    if status == 404:
        raise FetchError("not_found", is_immediately_retryable(reasons_cfg, "not_found"))
    if status >= 500:
        raise FetchError(
            "temporary_http_error", is_immediately_retryable(reasons_cfg, "temporary_http_error")
        )
    if status >= 400:
        raise FetchError("temporary_http_error", False)

    # requests는 서버가 Content-Type에 charset을 안 넣으면 ISO-8859-1로 잘못 단정한다 (잘 알려진
    # requests 함정). charset이 명시 안 된 경우에만 실제 바이트를 분석한 apparent_encoding으로
    # 바꿔서, 한국 사이트에서 흔한 "한글이 깨진 글자로 저장되는" 문제를 막는다.
    if "charset" not in response.headers.get("Content-Type", "").lower():
        response.encoding = response.apparent_encoding

    return FetchResult(html=response.text, final_url=response.url)
