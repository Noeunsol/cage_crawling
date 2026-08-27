from src.extraction.general_extractor import ExtractionError
from src.extraction.kin_parser import parse
from src.extraction.parser_registry import get_parser, get_source_category

SAMPLE_HTML = """
<html><body>
<div class="endTitleSection">질문 제목입니다</div>
<span class="infoItem"><span class="blind">작성일</span> 2026.08.27</span>
<div class="questionDetail">질문의 핵심 내용입니다. 카테고리 목록은 포함하지 않습니다.</div>
<div class="answerDetail">첫 번째 답변의 핵심 내용입니다.</div>
<div class="answerDetail">두 번째 답변입니다.<img alt="광고 이미지"></div>
<div class="related_questions">다른 질문 제목</div>
</body></html>
"""


def test_extracts_only_question_and_answers():
    result = parse(
        SAMPLE_HTML,
        "https://kin.naver.com/qna/detail.naver?d1id=1&dirId=1&docId=123",
        min_content_length=1,
    )
    assert result.title == "질문 제목입니다"
    assert "질문의 핵심" in result.content
    assert "첫 번째 답변" in result.content
    assert "다른 질문 제목" not in result.content
    assert result.published_date == "2026-08-27"


def test_rejects_category_pages():
    try:
        parse(SAMPLE_HTML, "https://kin.naver.com/qna/list.naver?dirId=1", 1)
        assert False, "ExtractionError가 발생해야 한다"
    except ExtractionError as error:
        assert error.reason == "extraction_failed"


def test_registry_routes_kin_and_uses_qna_threshold():
    assert get_parser("kin.naver.com") is parse
    assert get_source_category("kin.naver.com") == "qna"
