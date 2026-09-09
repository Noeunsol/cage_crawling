"""bobaedream.co.kr(모바일) 전용 파서 검증.

2026-09-08 실측: 범용 추출기(trafilatura)가 게시판 메뉴 목록과 실제 글 제목이 뒤섞인
컨테이너에서 제목을 엉뚱하게 뽑아, 서로 다른 글 여러 개가 전부 "커뮤니티 사이버매장"이라는
똑같은 제목으로 저장됐다. <title> 태그는 항상 정확했으므로 제목만 그걸로 교체한다.
"""

from src.extraction.bobaedream_parser import parse
from src.extraction.general_extractor import ExtractionError
from src.extraction.parser_registry import get_parser

SAMPLE_HTML = """
<html><head><title>화장실 유료화 차별 논란에 소송 나선 여성 - 보배드림 자유게시판</title></head>
<body>
<nav>자유게시판 게시판 베스트글 자유게시판 자동차뉴스/토론 정치·시사</nav>
<article>
<h1>커뮤니티 사이버매장</h1>
<p>화장실 사용료는 위치에 따라서 1유로 혹은 2유로를 남녀차별없이 받습니다. 화장실 사용하는
요금을 받아 챙기는 것에 대한 논란이 있습니다.</p>
<p>남자 무료 여자 유료라는 말은 이 글을 보고 처음 알았습니다. 실제로 소송까지 이어진 사례도
있다고 하니 상황이 심각해 보입니다.</p>
</article>
</body></html>
"""


def test_replaces_trafilatura_title_with_real_title_tag():
    result = parse(SAMPLE_HTML, url="https://m.bobaedream.co.kr/board/bbs_view/freeb/1", min_content_length=1)
    assert result.title == "화장실 유료화 차별 논란에 소송 나선 여성"
    assert "화장실 사용료" in result.content


def test_raises_extraction_error_when_body_too_short():
    html = '<html><head><title>짧은 글 - 보배드림 자유게시판</title></head><body><p>짧음</p></body></html>'
    try:
        parse(html, url="https://m.bobaedream.co.kr/x", min_content_length=1000)
        assert False, "ExtractionError가 발생해야 한다"
    except ExtractionError as e:
        assert e.reason == "extraction_too_short"


def test_get_parser_routes_bobaedream_to_dedicated_parser():
    assert get_parser("bobaedream.co.kr").dedicated is parse
    assert get_parser("m.bobaedream.co.kr").dedicated is parse
