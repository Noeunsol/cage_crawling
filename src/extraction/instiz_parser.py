"""Instiz 상세 게시글의 텍스트 본문만 추출한다."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from src.extraction.cleaner import clean_article_text, strip_noise_tags
from src.extraction.general_extractor import ExtractedContent, ExtractionError, build_or_raise


def parse(
    html: str, url: str, min_content_length: int, extraction_cfg: dict | None = None,
) -> ExtractedContent:
    if not re.search(r"/\d+/?$", urlsplit(url).path):
        raise ExtractionError("extraction_failed")

    soup = BeautifulSoup(html, "html.parser")
    title_meta = soup.select_one('meta[property="og:title"]')
    body_el = soup.select_one("article")
    if title_meta is None or body_el is None:
        raise ExtractionError("extraction_failed")

    strip_noise_tags(body_el)
    content = clean_article_text(
        body_el.get_text("\n", strip=True), (extraction_cfg or {}).get("cleaning", {}),
    )
    title = (title_meta.get("content") or "").strip()

    date_meta = soup.select_one('meta[property="article:published_time"]')
    date_match = re.match(r"\d{4}-\d{2}-\d{2}", date_meta.get("content", "")) if date_meta else None
    published_date = date_match.group(0) if date_match else None

    return build_or_raise(title, content, min_content_length, published_date)
