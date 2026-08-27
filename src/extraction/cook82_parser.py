"""82cook.com 전용 파서.

범용 추출기(trafilatura)가 원글보다 훨씬 긴 댓글 스레드를 본문으로 잘못 집어가고, 제목도
못 찾는 실패가 확인됐다(2026-08-26 실측) — 이 사이트 댓글이 trafilatura가 인식하는 일반적인
댓글 마크업 패턴이 아니라서 "본문이 아님"으로 걸러지지 않는다. 원글은 항상 ``div#articleBody``
안에 있어 그 컨테이너만 직접 본다.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.extraction.cleaner import clean_article_text, extract_dotted_date, strip_noise_tags, title_with_meta_fallback
from src.extraction.general_extractor import ExtractedContent, ExtractionError, build_or_raise


def parse(
    html: str, url: str, min_content_length: int, extraction_cfg: dict | None = None,
) -> ExtractedContent:
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("h4.title.bbstitle span")
    title_meta = soup.select_one('meta[property="og:title"]')
    title = title_with_meta_fallback(title_el, title_meta)

    body_el = soup.select_one("div#articleBody, div.articleBody")
    if body_el is None:
        raise ExtractionError("extraction_failed")
    strip_noise_tags(body_el)
    content = clean_article_text(
        body_el.get_text("\n", strip=True), (extraction_cfg or {}).get("cleaning", {}),
    )

    published_date = None
    date_el = soup.select_one(".readRight")
    if date_el:
        published_date = extract_dotted_date(date_el.get_text(strip=True), sep="-")

    return build_or_raise(title, content, min_content_length, published_date)
