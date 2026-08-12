"""사이트별 정적 parser rung — 네이버 지식인(__NEXT_DATA__), 범용 커뮤니티(본문+댓글).

실제 dcinside/fmkorea 전용 selector가 필요해지면 여기에 클래스를 추가하고
extract.router.SITE_PARSER_REGISTRY에 site_name→rung 이름을 등록한다.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..schema import ExtractedContent
from .base import _HANGUL, first_date, harvest_strings, title_of


class NaverKinExtractor:
    """네이버 지식인: __NEXT_DATA__ JSON 우선 파싱. 실패 시 None → 다음 rung."""
    name = "naver_kin"

    def extract(self, c, site, html) -> ExtractedContent | None:
        if not html or "kin.naver.com" not in c.domain:
            return None
        soup = BeautifulSoup(html, "lxml")
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag or not tag.string:
            return None
        try:
            data = json.loads(tag.string)
        except (ValueError, TypeError):
            return None
        chunks = [s for s in harvest_strings(data) if _HANGUL.search(s) and len(s) >= 15]
        if not chunks:
            return None
        title = title_of(soup) or chunks[0][:60]
        body = "\n".join(chunks)
        date = first_date(tag.string)
        return ExtractedContent(title=title, body_text=body, published_at=date,
                                published_at_source="html_parser" if date else "unknown")


class CommunityStaticParser:
    """커뮤니티 게시글의 본문 + 댓글을 generic bs4 selector로 추출 (정적).
    실제 dcinside/fmkorea는 차단·JS라 대개 None → playwright(렌더)로 escalate.
    ponytail: generic selector. 사이트별 정밀 selector는 여기에 클래스 추가로 확장."""
    name = "site_parser_community"
    _BODY_SEL = "article, .post, .write_div, .view_content, #content, .content, .board-content"

    def extract(self, c, site, html) -> ExtractedContent | None:
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")
        title = title_of(soup) or (c.title or "")
        body_el = soup.select_one(self._BODY_SEL)
        body = body_el.get_text("\n", strip=True) if body_el else ""
        image_urls = []
        for image in body_el.select("img") if body_el else []:
            src = image.get("data-original") or image.get("data-src") or image.get("src")
            if src and not src.startswith("data:"):
                image_urls.append(urljoin(c.source_url, src))
        image_urls = list(dict.fromkeys(image_urls))
        if not body and not image_urls:
            return None
        date = first_date(html)
        return ExtractedContent(title=title, body_text=body,
                                image_urls=image_urls,
                                published_at=date, published_at_source="html_parser" if date else "unknown")


class DcinsidePostExtractor:
    """DCInside 게시글 본문 parser. 댓글은 수집하지 않는다(본문만)."""
    name = "dcinside"

    def __init__(self, fetcher=None):
        self.fetcher = fetcher

    def extract(self, c, site, html) -> ExtractedContent | None:
        if not html or site.site_name != "dcinside":
            return None
        soup = BeautifulSoup(html, "lxml")
        if self.fetcher and not soup.select_one("#e_s_n_o") and hasattr(self.fetcher, "fetch"):
            retry_html = self.fetcher.fetch(c.source_url)
            if retry_html:
                soup = BeautifulSoup(retry_html, "lxml")
        body_el = soup.select_one(".write_div, .content")
        title_el = soup.select_one(".gallview_head .title_subject, .title_subject")
        date_el = soup.select_one(".gallview_head .gall_date, .gall_date")
        author_el = soup.select_one(".gallview_head .gall_writer")
        body = body_el.get_text("\n", strip=True) if body_el else ""
        image_urls = []
        for image in body_el.select("img") if body_el else []:
            src = image.get("data-original") or image.get("data-src") or image.get("src")
            if src and not src.startswith("data:"):
                image_urls.append(urljoin(c.source_url, src))
        image_urls = list(dict.fromkeys(image_urls))
        if not body and not image_urls:
            return None
        return ExtractedContent(
            title=title_el.get_text(" ", strip=True) if title_el else (c.title or ""),
            body_text=body,
            published_at=date_el.get("title") if date_el else None,
            published_at_source="html_parser",
            author_hint=author_el.get("data-nick") if author_el else None,
            image_urls=image_urls,
        )

