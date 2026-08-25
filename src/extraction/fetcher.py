"""원문 HTML을 가져온다. 실패 사유를 retry_policy.yaml의 reason code로 분류한다 (9.5, 11.5절)."""

from __future__ import annotations

from dataclasses import dataclass

import requests

from src.utils.rate_limit import throttle


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

    fetch_rate = retry_policy.get("rate_limit", {}).get("fetch", {})
    throttle("fetch", fetch_rate.get("min_interval_seconds", 0))

    try:
        response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    except requests.exceptions.Timeout as e:
        raise FetchError("timeout", retry_policy["reasons"]["timeout"]["retryable"]) from e
    except requests.exceptions.RequestException as e:
        raise FetchError(
            "temporary_http_error", retry_policy["reasons"]["temporary_http_error"]["retryable"]
        ) from e

    status = response.status_code
    if status in (401, 403):
        raise FetchError("access_denied", retry_policy["reasons"]["access_denied"]["retryable"])
    if status == 404:
        raise FetchError("not_found", retry_policy["reasons"]["not_found"]["retryable"])
    if status >= 500:
        raise FetchError(
            "temporary_http_error", retry_policy["reasons"]["temporary_http_error"]["retryable"]
        )
    if status >= 400:
        raise FetchError("temporary_http_error", False)

    # requests는 서버가 Content-Type에 charset을 안 넣으면 ISO-8859-1로 잘못 단정한다 (잘 알려진
    # requests 함정). charset이 명시 안 된 경우에만 실제 바이트를 분석한 apparent_encoding으로
    # 바꿔서, 한국 사이트에서 흔한 "한글이 깨진 글자로 저장되는" 문제를 막는다.
    if "charset" not in response.headers.get("Content-Type", "").lower():
        response.encoding = response.apparent_encoding

    return FetchResult(html=response.text, final_url=response.url)
