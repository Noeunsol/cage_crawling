"""네이버 지식iN 상세 질문에서 질문과 답변만 추출한다."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from src.extraction.cleaner import clean_article_text, extract_dotted_date, strip_noise_tags
from src.extraction.general_extractor import ExtractedContent, ExtractionError, build_or_raise


def parse(
    html: str, url: str, min_content_length: int, extraction_cfg: dict | None = None,
) -> ExtractedContent:
    parts = urlsplit(url)
    if parts.path != "/qna/detail.naver" or not parse_qs(parts.query).get("docId"):
        raise ExtractionError("extraction_failed")

    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one(".endTitleSection")
    question_el = soup.select_one(".questionDetail")
    if title_el is None or question_el is None:
        raise ExtractionError("extraction_failed")

    sections = [question_el, *soup.select(".answerDetail")]
    for section in sections:
        strip_noise_tags(section)
    text = "\n\n".join(section.get_text("\n", strip=True) for section in sections)
    content = clean_article_text(text, (extraction_cfg or {}).get("cleaning", {}))

    published_date = None
    for date_el in soup.select(".infoItem"):
        text_content = date_el.get_text(" ", strip=True)
        if "작성일" not in text_content:
            continue
        published_date = extract_dotted_date(text_content)
        break

    title = title_el.get_text(" ", strip=True)
    return build_or_raise(title, content, min_content_length, published_date)
