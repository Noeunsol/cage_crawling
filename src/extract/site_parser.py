"""사이트별 정적 parser rung — 네이버 지식인(__NEXT_DATA__), 범용 커뮤니티(본문+댓글).

실제 dcinside/fmkorea 전용 selector가 필요해지면 여기에 클래스를 추가하고
extract.router.SITE_PARSER_REGISTRY에 site_name→rung 이름을 등록한다.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..schema import ExtractedContent
from .base import _HANGUL, first_date, title_of


class NaverKinExtractor:
    """네이버 지식인 Q&A 전용 parser.

    ``__NEXT_DATA__`` 전체 문자열을 합치면 메뉴·배너·이미지 alt·중복 답변까지 섞인다.
    질문과 답변 DOM을 먼저 읽고, 구조가 바뀐 페이지에서만 이름 있는 JSON 필드를 보조로 쓴다.
    """
    name = "naver_kin"
    _QUESTION_SELECTORS = (
        ".question-content ._endContentsText", ".question-content__inner",
        ".c-question__content", ".question-content", "[class*='question'] [class*='content']",
    )
    _ANSWER_SELECTORS = (
        ".answer-content ._endContentsText", ".answer-content__inner",
        ".c-answer__content", ".answer-content", "[class*='answer'] [class*='content']",
    )
    _IMAGE_OR_UI = re.compile(r"^(?:이미지|사진|답변하기|채택하기|공유|신고|목록|더보기)$")

    @classmethod
    def _normalise(cls, value) -> str:
        if not isinstance(value, str):
            return ""
        text = BeautifulSoup(value, "lxml").get_text(" ", strip=True)
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 15 or not _HANGUL.search(text) or cls._IMAGE_OR_UI.match(text):
            return ""
        return text

    @classmethod
    def _texts(cls, soup, selectors, limit: int) -> list[str]:
        values: list[str] = []
        for selector in selectors:
            for element in soup.select(selector):
                # 본문에 들어간 이미지/버튼/스크립트는 구조적으로 제거한다.
                for noise in element.select("img, picture, svg, button, script, style, iframe"):
                    noise.decompose()
                text = cls._normalise(element.get_text(" ", strip=True))
                if text and text not in values:
                    values.append(text)
                    if len(values) >= limit:
                        return values
            if values:
                return values
        return values

    @classmethod
    def _json_texts(cls, data, kind: str, limit: int = 3) -> list[str]:
        """DOM 없는 신형 페이지 fallback: question/answer 이름의 본문 필드만 선택."""
        values: list[str] = []
        wanted = ("question", "inquiry") if kind == "question" else ("answer", "reply")
        content_words = ("content", "body", "detail", "text")

        def walk(node):
            if len(values) >= limit:
                return
            if isinstance(node, dict):
                for key, value in node.items():
                    normalized = key.lower().replace("_", "")
                    if any(word in normalized for word in wanted) and any(word in normalized for word in content_words):
                        text = cls._normalise(value)
                        if text and text not in values:
                            values.append(text)
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(data)
        return values

    def extract(self, c, site, html) -> ExtractedContent | None:
        if not html or "kin.naver.com" not in c.domain:
            return None
        soup = BeautifulSoup(html, "lxml")
        question_parts = self._texts(soup, self._QUESTION_SELECTORS, limit=1)
        answer_parts = self._texts(soup, self._ANSWER_SELECTORS, limit=3)
        tag = soup.find("script", id="__NEXT_DATA__")
        data = None
        if tag and tag.string:
            try:
                data = json.loads(tag.string)
            except (ValueError, TypeError):
                data = None
        if not question_parts and data is not None:
            question_parts = self._json_texts(data, "question", limit=1)
        if not answer_parts and data is not None:
            answer_parts = self._json_texts(data, "answer", limit=3)
        if not question_parts and not answer_parts:
            return None
        question_body = "\n".join(question_parts)
        answer_body = "\n\n".join(answer_parts)
        core_text = "\n\n".join(part for part in (
            f"질문: {question_body}" if question_body else "",
            f"답변: {answer_body}" if answer_body else "",
        ) if part)
        title = title_of(soup) or question_body[:60] or answer_body[:60]
        date = first_date(tag.string if tag and tag.string else html)
        return ExtractedContent(title=title, body_text=core_text,
                                question_body=question_body, answer_body=answer_body, core_text=core_text,
                                published_at=date,
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
