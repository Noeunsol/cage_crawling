"""정적 HTML fetcher. 정적 추출 rung들이 이 HTML을 공유한다.

파일명이 `http`가 아닌 이유: stdlib `http`와의 혼동 방지.
정중한 수집: UA 헤더, timeout, per-domain delay, robots.txt 준수(기본 on).
"""
from __future__ import annotations

import logging
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

_DEFAULT_UA = "Mozilla/5.0 (compatible; taxonomy-research-crawler/0.1; +contact@example.com)"


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

    def fetch(self, url: str, min_delay: float | None = None) -> str | None:
        """정적 HTML 반환. robots 불허/차단/오류면 None."""
        self.last_failure = None
        domain = urlparse(url).netloc
        if self.respect_robots and not self._allowed(url, domain):
            self.last_failure = "robots_disallowed"
            log.info("robots disallow → skip %s", url)
            return None
        self._throttle(domain, min_delay)
        try:
            resp = self._session.get(url, timeout=self.timeout)
        except requests.RequestException as e:
            self.last_failure = "network_error"
            log.info("fetch 실패 %s: %s", url, e)
            return None
        if resp.status_code != 200 or not resp.text:
            self.last_failure = f"http_{resp.status_code}"
            log.info("fetch status=%s %s", resp.status_code, url)
            return None
        return resp.text

    def fetch_bytes(self, url: str) -> bytes | None:
        """이미지 등 바이너리 반환. HTML과 같은 robots/throttle 정책을 쓴다."""
        self.last_failure = None
        domain = urlparse(url).netloc
        if self.respect_robots and not self._allowed(url, domain):
            self.last_failure = "robots_disallowed"
            return None
        self._throttle(domain)
        try:
            response = self._session.get(url, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException:
            self.last_failure = "network_error"
            return None
        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
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
