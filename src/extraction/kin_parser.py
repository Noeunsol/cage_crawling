"""네이버 지식iN 상세 질문에서 질문과 답변만 추출한다."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from src.extraction.cleaner import clean_article_text, extract_dotted_date, strip_noise_tags
from src.extraction.general_extractor import ExtractedContent, ExtractionError, build_or_raise

_HANGUL = re.compile(r"[가-힣]")
_IMAGE_OR_UI = re.compile(r"^(?:이미지|사진|답변하기|채택하기|공유|신고|목록|더보기)$")
_URL = re.compile(r"https?://\S+")
_CONTENT_WORDS = ("content", "body", "detail", "text")


def _normalize_json_text(value) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(_URL.sub("", value).split())
    if len(text) < 15 or not _HANGUL.search(text) or _IMAGE_OR_UI.match(text):
        return ""
    return text


def _json_texts(node, wanted: tuple[str, ...], limit: int) -> list[str]:
    values: list[str] = []

    def walk(n):
        if len(values) >= limit:
            return
        if isinstance(n, dict):
            for key, value in n.items():
                normalized = key.lower().replace("_", "")
                if any(w in normalized for w in wanted) and any(w in normalized for w in _CONTENT_WORDS):
                    text = _normalize_json_text(value)
                    if text and text not in values:
                        values.append(text)
                walk(value)
        elif isinstance(n, list):
            for value in n:
                walk(value)

    walk(node)
    return values


def _from_next_data(soup: BeautifulSoup) -> tuple[str, str]:
    """DOM 셀렉터가 안 먹히는 신형 페이지 대비: ``__NEXT_DATA__`` JSON에서 question/answer 필드를 직접 찾는다."""
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return "", ""
    try:
        data = json.loads(tag.string)
    except (ValueError, TypeError):
        return "", ""
    question_parts = _json_texts(data, ("question", "inquiry"), limit=1)
    answer_parts = _json_texts(data, ("answer", "reply"), limit=3)
    text = "\n\n".join(part for part in ("\n".join(question_parts), "\n\n".join(answer_parts)) if part)
    title = (question_parts[0] if question_parts else answer_parts[0] if answer_parts else "")[:60]
    return title, text


def parse(
    html: str, url: str, min_content_length: int, extraction_cfg: dict | None = None,
) -> ExtractedContent:
    parts = urlsplit(url)
    if parts.path != "/qna/detail.naver" or not parse_qs(parts.query).get("docId"):
        raise ExtractionError("extraction_empty")

    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one(".endTitleSection")
    question_el = soup.select_one(".questionDetail")

    if title_el is not None and question_el is not None:
        sections = [question_el, *soup.select(".answerDetail")]
        for section in sections:
            strip_noise_tags(section)
        title = title_el.get_text(" ", strip=True)
        text = "\n\n".join(section.get_text("\n", strip=True) for section in sections)
    else:
        title, text = _from_next_data(soup)
        if not title or not text:
            raise ExtractionError("extraction_empty")

    content = clean_article_text(text, (extraction_cfg or {}).get("cleaning", {}))

    published_date = None
    for date_el in soup.select(".infoItem"):
        text_content = date_el.get_text(" ", strip=True)
        if "작성일" not in text_content:
            continue
        published_date = extract_dotted_date(text_content)
        break

    return build_or_raise(title, content, min_content_length, published_date)
