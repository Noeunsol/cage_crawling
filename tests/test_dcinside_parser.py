"""gall.dcinside.com 전용 파서 검증 (2026-08-26: 범용 추출기가 갤러리 목록을 본문으로
잘못 집어가던 문제 때문에 추가됨)."""

from src.extraction.dcinside_parser import parse
from src.extraction.general_extractor import ExtractionError
from src.extraction.parser_registry import get_parser

SAMPLE_HTML = """
<html><body>
<div class="gall_list">다른 글 제목 1</div>
<div class="gall_list">다른 글 제목 2</div>
<span class="title_subject">실제 게시글 제목</span>
<span class="gall_date" title="2026-08-26 13:15:02">2026.08.26 13:15:02</span>
<div class="write_div">
  <p>본문 첫 문단입니다. 충분히 긴 실제 게시글 내용을 담고 있습니다.</p>
  <p><img src="https://dcimg.dcinside.co.kr/x.jpg"></p>
  <script>window.OutLink.renderOutLinkWarning('#x');</script>
  <p>본문 둘째 문단입니다.</p>
</div>
</body></html>
"""


def test_parses_title_body_date_ignoring_sidebar_list():
    result = parse(SAMPLE_HTML, url="https://gall.dcinside.com/board/view/?id=x&no=1", min_content_length=1)
    assert result.title == "실제 게시글 제목"
    assert "다른 글 제목" not in result.content
    assert "본문 첫 문단" in result.content
    assert "본문 둘째 문단" in result.content
    assert "OutLink" not in result.content
    assert result.published_date == "2026-08-26"


def test_raises_extraction_error_when_write_div_missing():
    try:
        parse("<html><body>본문 없음</body></html>", url="https://gall.dcinside.com/x", min_content_length=1)
        assert False, "ExtractionError가 발생해야 한다"
    except ExtractionError as e:
        assert e.reason == "extraction_failed"


def test_get_parser_routes_dcinside_to_dedicated_parser():
    assert get_parser("gall.dcinside.com") is parse
