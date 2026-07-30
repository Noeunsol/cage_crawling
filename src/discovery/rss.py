"""rss — 뉴스 RSS discovery.

- discover(): 키워드 모드(레거시). task.seed_urls의 피드에서 링크만 추출.
- discover_news_trend(): 트렌드 모드. config feed 목록에서 pubDate 파싱 + 수집 윈도우 필터 + meta 태깅.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from ..schema import UrlCandidate
from .base import feed_items, feed_links, make_candidate, parse_feed_date

log = logging.getLogger(__name__)


def discover(task, subtype, registry) -> list:
    out = []
    for feed in task.seed_urls:
        for u in feed_links("rss", feed):
            if u and u.startswith("http"):
                out.append(make_candidate(registry, u, task, "rss", subtype))
    return out


def discover_news_trend(feeds, registry, exclude_older_than_days: int = 14) -> list:
    """뉴스 RSS 트렌드 수집. 윈도우 초과 항목은 제외. 각 feed는 {press, category, url}."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=exclude_older_than_days)
    out = []
    for feed in feeds:
        try:
            items = feed_items(feed["url"])
        except Exception as e:  # noqa: BLE001 (피드 하나 실패해도 나머지 진행)
            log.info("RSS feed fetch 실패 %s (%s)", feed.get("url"), e)
            continue
        for it in items:
            link = it.get("link")
            if not link or not link.startswith("http"):
                continue
            dt = parse_feed_date(it.get("published_at"))
            if dt and dt < cutoff:   # 수집 윈도우 초과 제외
                continue
            domain = urlparse(link).netloc.lower()
            info = registry.lookup(domain)
            cand = UrlCandidate(
                link, domain, f"rss:{feed.get('press', '')}", "rss", "", "",
                title=it.get("title"), canonical_url=link, site_name=info.site_name,
                site_type="news", collection_type="news_case", discovery_method="rss",
            )
            cand.published_at_hint = dt.isoformat() if dt else None
            cand.snippet = it.get("summary")
            cand.meta = {
                "source": "news_rss", "source_type": "news",
                "board_name": feed.get("press", ""), "category_name": feed.get("category", ""),
                "bucket": "latest_news", "is_trending": False,
            }
            out.append(cand)
    return out
