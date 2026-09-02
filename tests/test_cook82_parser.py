"""82cook.com 전용 파서 검증 (2026-08-26: 범용 추출기가 원글보다 긴 댓글을 본문으로
잘못 집어가고 제목도 못 찾던 문제 때문에 추가됨)."""

from src.extraction.cook82_parser import parse
from src.extraction.general_extractor import ExtractionError
from src.extraction.parser_registry import get_parser

SAMPLE_HTML = """
<html><body>
<h4 class="title bbstitle"><i class="icon-doc-text"></i><span>실제 게시글 제목</span></h4>
<div id="readHead">
  <div class="readRight">작성일 : 2026-08-25 21:52:11</div>
</div>
<div id="articleBody">
  <p>본문 첫 문단입니다. 충분히 긴 실제 게시글 내용을 담고 있습니다.</p>
  <p><img src="https://cdn.82cook.com/x.jpg"></p>
  <p>본문 둘째 문단입니다.</p>
</div>
<div class="read_reple">
  <div class="rp"><span class="title">1. 댓글쓴이</span><p>이건 댓글이라 본문에 들어가면 안 됩니다.</p></div>
  <div class="rp"><span class="title">2. 다른댓글쓴이</span><p>댓글이 원글보다 훨씬 깁니다 여러 줄에 걸쳐서요 아무튼 이 텍스트는 절대 본문으로 취급되면 안 됩니다.</p></div>
</div>
</body></html>
"""


def test_parses_title_body_date_ignoring_comment_thread():
    result = parse(SAMPLE_HTML, url="https://www.82cook.com/entiz/read.php?num=1", min_content_length=1)
    assert result.title == "실제 게시글 제목"
    assert "댓글" not in result.content
    assert "본문 첫 문단" in result.content
    assert "본문 둘째 문단" in result.content
    assert result.published_date == "2026-08-25"


def test_raises_extraction_error_when_article_body_missing():
    try:
        parse("<html><body>본문 없음</body></html>", url="https://www.82cook.com/x", min_content_length=1)
        assert False, "ExtractionError가 발생해야 한다"
    except ExtractionError as e:
        assert e.reason == "extraction_empty"


def test_get_parser_routes_82cook_domains_to_dedicated_parser():
    assert get_parser("82cook.com") is parse
    assert get_parser("www.82cook.com") is parse
