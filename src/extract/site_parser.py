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
    _COMMENT_SEL = ".comment, .cmt, .comment_box, li.comment, .reply, .cmt_txt"

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
        comments = [e.get_text(" ", strip=True) for e in soup.select(self._COMMENT_SEL)]
        comments = [x for x in comments if x]
        if not body and not comments and not image_urls:
            return None
        date = first_date(html)
        return ExtractedContent(title=title, body_text=body, comments=comments,
                                comment_count=len(comments),
                                image_urls=image_urls,
                                published_at=date, published_at_source="html_parser" if date else "unknown")


class DcinsidePostExtractor:
    """DCInside 게시글 본문과 AJAX 댓글·대댓글 parser."""
    name = "dcinside"

    def __init__(self, fetcher=None, max_comments: int = 200):
        self.fetcher = fetcher
        self.max_comments = max_comments

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
        comments = [
            element.get_text(" ", strip=True)
            for element in soup.select(".cmt_list .usertxt, .comment")
            if element.get_text(" ", strip=True)
        ]
        if self.fetcher:
            comments = self._fetch_comments(c.source_url, soup) or comments
        if not body and not comments and not image_urls:
            return None
        return ExtractedContent(
            title=title_el.get_text(" ", strip=True) if title_el else (c.title or ""),
            body_text=body,
            comments=comments,
            comment_count=len(comments),
            published_at=date_el.get("title") if date_el else None,
            published_at_source="html_parser",
            author_hint=author_el.get("data-nick") if author_el else None,
            image_urls=image_urls,
        )

    def _fetch_comments(self, article_url: str, soup: BeautifulSoup) -> list[str]:
        query = parse_qs(urlsplit(article_url).query)
        gallery_id = (query.get("id") or [""])[0]
        article_no = (query.get("no") or [""])[0]
        token = soup.select_one("#e_s_n_o")
        if not gallery_id or not article_no or not token:
            return []
        gall_type = soup.select_one("#_GALLTYPE_")
        secret = soup.select_one("#secret_article_key")
        endpoint = f"{urlsplit(article_url).scheme}://{urlsplit(article_url).netloc}/board/comment/"
        out: list[str] = []
        page = 1
        while len(out) < self.max_comments:
            payload = {
                "id": gallery_id, "no": article_no,
                "cmt_id": gallery_id, "cmt_no": article_no,
                "focus_cno": "", "focus_pno": "",
                "e_s_n_o": token.get("value", ""),
                "comment_page": str(page), "sort": "D", "prevCnt": "",
                "board_type": "",
                "_GALLTYPE_": gall_type.get("value", "G") if gall_type else "G",
                "secret_article_key": secret.get("value", "") if secret else "",
                "clean": "", "nptest": "",
            }
            data = self.fetcher.post_json(endpoint, payload, article_url)
            rows = (data or {}).get("comments") or []
            if not rows:
                break
            for row in rows:
                if row.get("del_yn") == "Y":
                    continue
                text = BeautifulSoup(str(row.get("memo") or ""), "lxml").get_text(" ", strip=True)
                if text:
                    out.append(("[대댓글] " if row.get("c_no") else "[댓글] ") + text)
                if len(out) >= self.max_comments:
                    break
            if len(rows) >= int((data or {}).get("total_cnt") or 0):
                break
            page += 1
        return out
