from src.extraction.general_extractor import ExtractionError
from src.extraction.instiz_parser import parse
from src.extraction.parser_registry import get_parser, get_source_category

SAMPLE_HTML = """
<html><head>
<meta property="og:title" content="실제 게시글 제목">
<meta property="article:published_time" content="2026-08-27T13:42:03+09:00">
</head><body>
<nav>다른 게시글 목록</nav>
<article>게시글의 텍스트 본문입니다. 댓글과 목록은 제외합니다.<img alt="이미지"></article>
<div class="comments">댓글 내용</div>
</body></html>
"""


def test_extracts_only_detail_article():
    result = parse(SAMPLE_HTML, "https://www.instiz.net/name/123", 1)
    assert result.title == "실제 게시글 제목"
    assert "텍스트 본문" in result.content
    assert "다른 게시글" not in result.content
    assert "댓글 내용" not in result.content
    assert result.published_date == "2026-08-27"


def test_rejects_list_page():
    try:
        parse(SAMPLE_HTML, "https://www.instiz.net/name", 1)
        assert False, "ExtractionError가 발생해야 한다"
    except ExtractionError as error:
        assert error.reason == "extraction_empty"


def test_registry_routes_instiz_and_uses_community_threshold():
    assert get_parser("www.instiz.net") is parse
    assert get_source_category("www.instiz.net") == "community"
