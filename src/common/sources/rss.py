"""뉴스 RSS 수집 — feed 목록에서 pubDate 파싱 + 수집 윈도우 필터 + meta 태깅. 1차 전용.

XML 피드 파싱 헬퍼(parse_feed_date/feed_items)도 여기 있다 — 유일한 사용처라 파일을 나누지 않는다.
"""
from __future__ import annotations

import email.utils
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from src.common.schema import UrlCandidate

log = logging.getLogger(__name__)


def parse_feed_date(text: str | None) -> datetime | None:
    """RSS pubDate(RFC822) 또는 Atom updated(ISO) → tz-aware datetime. 실패 시 None."""
    if not text:
        return None
    text = text.strip()
    try:
        dt = email.utils.parsedate_to_datetime(text)   # RFC822
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_XML_DECL = re.compile(rb"^\s*<\?xml[^>]*\?>")
_XML_ENCODING = re.compile(rb"""encoding=["']([\w.-]+)["']""")


def _decode_xml(response) -> str:
    """XML 선언의 인코딩으로 디코드한 뒤 선언부를 제거한다.

    ET는 euc-kr 등 multi-byte 바이트를 직접 파싱하지 못하고(국내 매체에 흔하다),
    requests는 charset 헤더가 없으면 ISO-8859-1로 넘겨 한글이 깨진다. 선언부가 정본이다.
    """
    raw = response.content
    declaration = _XML_DECL.match(raw)
    encoding = None
    if declaration:
        found = _XML_ENCODING.search(declaration.group())
        encoding = found.group(1).decode("ascii", "ignore") if found else None
    encoding = encoding or response.encoding or "utf-8"
    try:
        text = raw.decode(encoding, "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")
    return _XML_DECL.sub(b"", raw, count=1).decode(encoding, "replace").lstrip() if declaration else text


def feed_items(url: str) -> list[dict]:
    """RSS <item> / Atom <entry>에서 link·title·published_at을 추출한다(날짜 필터용)."""
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    root = ET.fromstring(_decode_xml(response))
    items: list[dict] = []
    for it in root.findall(".//item"):   # RSS 2.0 (보통 무네임스페이스)
        pub = it.findtext("pubDate") or it.findtext("{*}date")
        items.append({
            "link": it.findtext("link"), "title": it.findtext("title"),
            "summary": it.findtext("description") or it.findtext("{*}summary"),
            "published_at": pub,
        })
    for en in root.findall(".//{*}entry"):   # Atom
        link_el = en.find("{*}link")
        link = link_el.get("href") if link_el is not None else None
        pub = en.findtext("{*}updated") or en.findtext("{*}published")
        items.append({
            "link": link, "title": en.findtext("{*}title"),
            "summary": en.findtext("{*}summary") or en.findtext("{*}content"),
            "published_at": pub,
        })
    return items



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
