"""bobaedream.co.kr(모바일) 전용 파서.

범용 추출기(trafilatura)가 페이지 상단의 게시판 메뉴 목록과 실제 글 제목이 한 컨테이너에
뒤섞여 있어서 제목을 엉뚱하게 뽑는 실패가 확인됐다(2026-09-08 실측: "커뮤니티 사이버매장"이라는
무의미한 문자열이 서로 다른 여러 글의 제목으로 똑같이 반복됨 — 실제로는 화장실 유료화 차별,
인종차별 논란 등 완전히 다른 글들이었다). <title> 태그는 매번 정확했으므로 제목만 그걸로
교체하고, 본문은 trafilatura 결과를 그대로 쓴다.
"""

from __future__ import annotations

import re

from src.extraction.cleaner import title_from_html_tag
from src.extraction.general_extractor import ExtractedContent, extract as general_extract

_SITE_SUFFIX = re.compile(r"\s*-\s*보배드림[^-]*$")


def parse(
    html: str, url: str, min_content_length: int, extraction_cfg: dict | None = None,
) -> ExtractedContent:
    result = general_extract(html, url, min_content_length, extraction_cfg)
    real_title = _SITE_SUFFIX.sub("", title_from_html_tag(html)).strip()
    if real_title:
        result.title = real_title
    return result
