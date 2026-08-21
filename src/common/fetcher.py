"""정적 HTML fetcher. 정적 추출 rung들이 이 HTML을 공유한다.

파일명이 `http`가 아닌 이유: stdlib `http`와의 혼동 방지.
정중한 수집: UA 헤더, timeout, per-domain delay, robots.txt 준수(기본 on).
"""
from __future__ import annotations

import logging
import re
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

# <meta charset="euc-kr"> / <meta ... content="text/html; charset=euc-kr">
_META_CHARSET = re.compile(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)


def _fix_encoding(resp) -> None:
    """Content-Type에 charset이 없으면 requests는 ISO-8859-1로 가정한다(RFC 2616).

    국내 매체는 charset을 헤더가 아니라 <meta>로만 선언하는 곳이 많다. 그대로 두면
    본문이 통째로 깨져(°æÂû ¡°ÀÌÀç¸í…) 한글 비율 0 → 한국 관련성 0이 되고,
    멀쩡한 국내 기사가 비한국 콘텐츠로 버려진다(실측 2026-08-19: mbn·shimlee 등).
    """
    if "charset=" in (resp.headers.get("content-type") or "").lower():
        return
    match = _META_CHARSET.search(resp.content[:4096])
    if match:
        resp.encoding = match.group(1).decode("ascii", "ignore")
    else:   # 선언이 아예 없으면 바이트로 추정한다(charset_normalizer)
        resp.encoding = resp.apparent_encoding or resp.encoding


_DEFAULT_UA ="Mozilla/5.0 (compatible; taxonomy-research-crawler/0.1; +contact@example.com)"
_BROWSER_IMAGE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138 Safari/537.36"
)


class Fetcher:
    def __init__(self, settings: dict | None = None):
        h = (settings or {}).get("http", {})
        self.timeout = h.get("timeout", 20)
        self.user_agent = h.get("user_agent", _DEFAULT_UA)
        self.per_domain_delay = h.get("per_domain_delay", 1.0)
        self.respect_robots = h.get("respect_robots", True)
        self._last_hit: dict[str, float] = {}       # domain → 마지막 요청 시각
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.user_agent,
                                      "Accept-Language": "ko,en;q=0.8"})
        self.last_failure: str | None = None
        # 같은 도메인이 연속으로 막히면 더 두드리지 않는다. 차단을 키우고 시간만 버린다.
        # 실측: 디시가 200+빈본문으로 막았는데 114번을 계속 요청했다(2026-08-19).
        self.max_consecutive_failures = int(h.get("max_consecutive_failures", 5))
        self._fail_streak: dict[str, int] = {}

    def fetch(self, url: str, min_delay: float | None = None) -> str | None:
        """정적 HTML 반환. robots 불허/차단/오류면 None."""
        self.last_failure = None
        domain = urlparse(url).netloc
        if self._fail_streak.get(domain, 0) >= self.max_consecutive_failures:
            self.last_failure = f"domain_blocked_after_{self.max_consecutive_failures}_failures"
            return None
        if self.respect_robots and not self._allowed(url, domain):
            self.last_failure = "robots_disallowed"
            log.info("robots disallow → skip %s", url)
            return None
        self._throttle(domain, min_delay)
        try:
            resp = self._session.get(url, timeout=self.timeout)
        except requests.RequestException as e:
            self.last_failure = "network_error"
            self._note_failure(domain)
            log.info("fetch 실패 %s: %s", url, e)
            return None
        if resp.status_code != 200:
            self.last_failure = f"http_{resp.status_code}"
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                self._note_failure(domain)      # 차단·과부하 신호만 센다(404는 그 URL 문제)
            log.info("fetch status=%s %s", resp.status_code, url)
            return None
        _fix_encoding(resp)
        if not resp.text:
            # 200인데 본문이 비어 있으면 대개 봇 차단·레이트리밋이다(디시가 이렇게 막는다).
            # http_200으로 기록하면 읽는 사람이 원인을 못 찾는다.
            self.last_failure = "empty_body"
            self._note_failure(domain)
            log.info("fetch 200 but empty body (봇 차단·레이트리밋 의심) %s", url)
            return None
        self._fail_streak.pop(domain, None)
        return resp.text

    def _note_failure(self, domain: str) -> None:
        streak = self._fail_streak.get(domain, 0) + 1
        self._fail_streak[domain] = streak
        if streak == self.max_consecutive_failures:
            log.warning("%s 연속 %d회 실패 — 이번 실행에서는 더 요청하지 않는다", domain, streak)

    def fetch_bytes(self, url: str) -> bytes | None:
        """이미지 등 바이너리 반환. HTML과 같은 robots/throttle 정책을 쓴다."""
        self.last_failure = None
        domain = urlparse(url).netloc
        if self.respect_robots and not self._allowed(url, domain):
            self.last_failure = "robots_disallowed"
            return None
        self._throttle(domain)
        headers = {}
        if domain.endswith("dcinside.co.kr"):
            # dcimg CDN은 일반 브라우저 이미지 요청이 아니면 존재하는 URL도 404로 응답한다.
            headers = {
                "User-Agent": _BROWSER_IMAGE_UA,
                "Referer": "https://gall.dcinside.com/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            }
        try:
            response = self._session.get(url, timeout=self.timeout, headers=headers)
            response.raise_for_status()
        except requests.RequestException:
            self.last_failure = "network_error"
            return None
        content_type = response.headers.get("Content-Type", "")
        if not (content_type.startswith("image/") or content_type.startswith("application/octet-stream")):
            self.last_failure = "not_image"
            return None
        return response.content

    def post_json(self, url: str, data: dict, referer: str) -> dict | None:
        """같은 사이트의 읽기 전용 AJAX endpoint 호출."""
        self.last_failure = None
        parsed = urlparse(url)
        domain = parsed.netloc
        if self.respect_robots and not self._allowed(url, domain):
            self.last_failure = "robots_disallowed"
            return None
        self._throttle(domain)
        headers = {
            "Referer": referer,
            "Origin": f"{parsed.scheme}://{domain}",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        for attempt in range(2):
            try:
                response = self._session.post(url, data=data, headers=headers, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt == 0:
                    self._throttle(domain)
                    continue
                self.last_failure = "ajax_error"
                log.info("AJAX fetch 실패 %s: %s", url, exc)
        return None

    def _throttle(self, domain: str, min_delay: float | None = None) -> None:
        delay = max(self.per_domain_delay, min_delay or 0)
        last = self._last_hit.get(domain)
        if last is not None:
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[domain] = time.monotonic()

    def _allowed(self, url: str, domain: str) -> bool:
        rp = self._robots.get(domain, "missing")
        if rp == "missing":
            rp = self._load_robots(url, domain)
            self._robots[domain] = rp
        if rp is None:            # robots 못 읽으면 허용(관대)
            return True
        return rp.can_fetch(self.user_agent, url)

    def _load_robots(self, url: str, domain: str):
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{domain}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = self._session.get(robots_url, timeout=self.timeout)
            if resp.status_code != 200:
                return None
            rp.parse(resp.text.splitlines())
            return rp
        except requests.RequestException:
            return None
