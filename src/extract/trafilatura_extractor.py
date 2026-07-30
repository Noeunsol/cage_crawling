"""trafilatura rung — 뉴스·블로그·범용. 본문 + 메타(제목/날짜) 추출."""
from __future__ import annotations

import trafilatura

from ..schema import ExtractedContent
from .base import title_of_html


class TrafilaturaExtractor:
    name = "trafilatura"

    def extract(self, c, site, html) -> ExtractedContent | None:
        if not html:
            return None
        body = trafilatura.extract(html, include_comments=False, favor_recall=True)
        if not body:
            return None
        meta = trafilatura.extract_metadata(html)
        title = (meta.title if meta and meta.title else None) or title_of_html(html) or (c.title or "")
        date = meta.date if meta and meta.date else None
        return ExtractedContent(title=title, body_text=body, published_at=date,
                                published_at_source="metadata" if date else "unknown")
