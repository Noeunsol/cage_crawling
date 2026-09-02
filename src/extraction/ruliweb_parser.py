"""ruliweb.com(bbs.ruliweb.com) 전용 파서.

범용 추출기(trafilatura)가 실제 게시글 본문 대신 옆 게시판 목록 표나 "키워드 차단" 팝업 UI
텍스트를 본문으로 잘못 집어가는 실패율이 높게 확인됐다(2026-08-26 실측, board 300800 다수).
ruliweb은 본문이 항상 ``div.view_content`` 안에 있어 그 컨테이너만 직접 보는 게 더 확실하다.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.extraction.cleaner import clean_article_text, extract_dotted_date, strip_noise_tags, title_with_meta_fallback
from src.extraction.general_extractor import ExtractedContent, ExtractionError, build_or_raise


def parse(
    html: str, url: str, min_content_length: int, extraction_cfg: dict | None = None,
) -> ExtractedContent:
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("span.subject_inner_text")
    title_meta = soup.select_one('meta[property="og:title"]')
    title = title_with_meta_fallback(title_el, title_meta)

    body_el = soup.select_one("div.view_content, [itemprop='articleBody']")
    if body_el is None:
        raise ExtractionError("extraction_empty")
    strip_noise_tags(body_el)
    content = clean_article_text(
        body_el.get_text("\n", strip=True), (extraction_cfg or {}).get("cleaning", {}),
    )

    published_date = None
    date_el = soup.select_one("span.regdate")
    if date_el:
        published_date = extract_dotted_date(date_el.get_text(strip=True))

    return build_or_raise(title, content, min_content_length, published_date)
