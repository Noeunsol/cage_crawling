"""gated rung — Playwright(JS 렌더) / Firecrawl(유료 최후 수단). config로 enable, lib은 lazy import.

fetch는 Playwright, 추출은 trafilatura로 통일. Firecrawl은 value_score 게이트 통과 시에만 호출.
"""
from __future__ import annotations

import logging

import trafilatura

from ..schema import ExtractedContent
from .base import _GatedExtractor

log = logging.getLogger(__name__)


class PlaywrightExtractor(_GatedExtractor):
    """JS 렌더 후 trafilatura로 추출. 커뮤니티 등 동적 페이지용. lib 미설치면 None."""
    name = "playwright"

    def extract(self, c, site, html) -> ExtractedContent | None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.info("playwright 미설치 → skip")
            return None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent="Mozilla/5.0 (crawler)")
                page.goto(c.source_url, timeout=20000)
                rendered = page.content()
                browser.close()
        except Exception as e:  # noqa: BLE001 (렌더 실패는 다음 rung으로)
            log.info("playwright 렌더 실패 %s: %s", c.source_url, e)
            return None
        body = trafilatura.extract(rendered, favor_recall=True)
        if not body:
            return None
        meta = trafilatura.extract_metadata(rendered)
        title = (meta.title if meta and meta.title else None) or (c.title or "")
        date = meta.date if meta and meta.date else None
        return ExtractedContent(title=title, body_text=body, published_at=date,
                                published_at_source="metadata" if date else "unknown")


class FirecrawlExtractor(_GatedExtractor):
    """최후 수단(유료). value_score 게이트 통과 시에만 라우터가 호출."""
    name = "firecrawl"

    def extract(self, c, site, html) -> ExtractedContent | None:
        try:
            from firecrawl import FirecrawlApp
        except ImportError:
            log.info("firecrawl 미설치 → skip")
            return None
        try:
            app = FirecrawlApp()
            res = app.scrape_url(c.source_url, params={"formats": ["markdown"]})
            body = (res or {}).get("markdown") or ""
        except Exception as e:  # noqa: BLE001
            log.info("firecrawl 실패 %s: %s", c.source_url, e)
            return None
        if not body:
            return None
        return ExtractedContent(title=c.title or "", body_text=body,
                                published_at=None, published_at_source="unknown")
