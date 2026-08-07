"""Discovery 공통 헬퍼 — 후보 생성, XML 피드 파싱, 검색 provider 공통 호출.

각 discovery 모듈(board/rss/serpapi/...)은 이 헬퍼로 공통 UrlCandidate를 만든다.
API 결과의 snippet은 본문으로 저장하지 않는다 — Extractor가 실제 HTML에서 재추출.
"""
from __future__ import annotations

import email.utils
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from ..schema import UrlCandidate
from ..keyword_discovery.search import get_client, run_client


def make_candidate(registry, url, task, method, subtype, title=None) -> UrlCandidate:
    domain = urlparse(url).netloc.lower()
    info = registry.lookup(domain)
    return UrlCandidate(url, domain, "", method, task.taxonomy_lv2, subtype.name,
                        title=title, canonical_url=url, site_name=info.site_name,
                        site_type=info.site_type, collection_type=task.collection_type,
                        discovery_method=method)


def feed_links(method: str, url: str) -> list[str]:
    """RSS(<link>) / sitemap(<loc>) 피드에서 링크 목록을 뽑는다."""
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    if method == "sitemap":
        return [e.text for e in root.findall(".//{*}loc")]
    return [e.text or e.get("href") for e in root.findall(".//{*}link")]


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


def feed_items(url: str) -> list[dict]:
    """RSS <item> / Atom <entry>에서 link·title·published_at을 추출한다(날짜 필터용)."""
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    root = ET.fromstring(response.content)
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


def search_discover(api: str, method: str, task, subtype, registry, query_gen,
                    budget, max_urls) -> list[UrlCandidate]:
    """검색 provider(SerpAPI/Tavily/Exa) 공통 호출. client 미구현이면 빈 결과."""
    client = get_client(api)
    if client is None:
        return []
    queries = query_gen.generate_for_task(subtype, task, api)
    allowed = budget.take("queries", task.taxonomy_lv2, task.collection_type, len(queries))
    if allowed == 0:
        return []
    return run_client(client, queries, task.taxonomy_lv2, subtype, registry,
                      task.collection_type, discovery_method=method, limit=max_urls,
                      max_queries=allowed)
