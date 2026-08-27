"""ruliweb.com 전용 파서 검증 (2026-08-26: 범용 추출기가 옆 게시판 목록 표를 본문으로
잘못 집어가던 문제 때문에 추가됨)."""

from src.extraction.general_extractor import ExtractionError
from src.extraction.parser_registry import get_parser
from src.extraction.ruliweb_parser import parse

SAMPLE_HTML = """
<html><body>
<div class="board_list_table">
  <table><tr><td>7449</td><td>정치</td><td>다른 글 제목</td></tr></table>
</div>
<h4 class="subject">
  <span class="subject_text" itemprop="headline">
    <span class="category_text">[정치]</span>
    <span class="subject_inner_text">실제 게시글 제목입니다</span>
  </span>
</h4>
<p>작성일 <span class="regdate" itemprop="datePublished">2026.07.03 (17:40:40)</span></p>
<div class="view_content autolink" itemprop="articleBody">
  <p>본문 첫 문단입니다. 충분히 긴 실제 게시글 내용을 담고 있습니다.</p>
  <p><img src="https://i2.ruliweb.com/img/x.webp" alt="alt텍스트마커"></p>
  <p>본문 둘째 문단입니다. 이미지 설명이 아니라 진짜 작성자의 글입니다.</p>
  <iframe src="https://www.youtube.com/embed/xyz"></iframe>
</div>
</body></html>
"""


def test_parses_title_body_date_ignoring_sidebar_list():
    result = parse(SAMPLE_HTML, url="https://bbs.ruliweb.com/community/board/300800/read/1", min_content_length=1)
    assert result.title == "실제 게시글 제목입니다"
    assert "다른 글 제목" not in result.content
    assert "본문 첫 문단" in result.content
    assert "본문 둘째 문단" in result.content
    assert result.published_date == "2026-07-03"


def test_strips_images_and_iframes_from_body():
    result = parse(SAMPLE_HTML, url="https://bbs.ruliweb.com/community/board/300800/read/1", min_content_length=1)
    assert "youtube" not in result.content.lower()
    assert "alt텍스트마커" not in result.content


def test_raises_extraction_error_when_view_content_missing():
    try:
        parse("<html><body>본문 없음</body></html>", url="https://bbs.ruliweb.com/x", min_content_length=1)
        assert False, "ExtractionError가 발생해야 한다"
    except ExtractionError as e:
        assert e.reason == "extraction_failed"


def test_get_parser_routes_bbs_ruliweb_to_dedicated_parser():
    assert get_parser("bbs.ruliweb.com") is parse
