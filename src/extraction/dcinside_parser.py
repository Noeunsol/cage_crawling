"""gall.dcinside.com 전용 파서.

범용 추출기(trafilatura)가 실제 게시글 대신 갤러리 목록(다른 글 제목·공지 등)을 본문으로
잘못 집어가는 실패가 확인됐다(2026-08-26 실측, dcbest 갤러리). 본문은 항상
``div.write_div`` 안에 있어 그 컨테이너만 직접 본다.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.extraction.cleaner import clean_article_text, strip_noise_tags, title_with_meta_fallback
from src.extraction.general_extractor import ExtractedContent, ExtractionError, build_or_raise


def parse(
    html: str, url: str, min_content_length: int, extraction_cfg: dict | None = None,
) -> ExtractedContent:
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("span.title_subject")
    title_meta = soup.select_one('meta[property="og:title"]')
    title = title_with_meta_fallback(title_el, title_meta)

    body_el = soup.select_one("div.write_div, [itemprop='articleBody']")
    if body_el is None:
        raise ExtractionError("extraction_empty")
    strip_noise_tags(body_el)
    content = clean_article_text(
        body_el.get_text("\n", strip=True), (extraction_cfg or {}).get("cleaning", {}),
    )

    published_date = None
    date_el = soup.select_one("span.gall_date")
    if date_el and date_el.get("title"):
        published_date = date_el["title"].split(" ")[0]

    return build_or_raise(title, content, min_content_length, published_date)
