from src.common.extract.site_parser import NaverKinExtractor
from src.common.schema import UrlCandidate


def _candidate():
    return UrlCandidate(
        source_url="https://kin.naver.com/qna/detail.naver?d1id=7&docId=1",
        domain="kin.naver.com", search_query="자살 고민", search_api="serpapi",
        taxonomy_lv2_candidate="1_C_Self_Harm", subtype_candidate="suicide",
    )


def test_naver_kin_parser_keeps_only_question_and_answers():
    html = """
    <html><head><title>자살 생각이 들어요 : 지식iN</title></head><body>
      <nav>로그인 답변하기 공유하기</nav>
      <section class="question-content"><div class="_endContentsText">
        요즘 너무 힘들고 자살 생각이 자꾸 들어서 어떻게 해야 할지 모르겠습니다.
        <img alt="광고 이미지" src="ad.jpg" />
      </div></section>
      <section class="answer-content"><div class="_endContentsText">
        혼자 감당하지 말고 가까운 응급실이나 정신건강 전문가에게 바로 도움을 요청하세요.
      </div></section>
      <section class="answer-content"><div class="_endContentsText">
        믿을 수 있는 가족이나 친구에게 현재 상태를 알리고 곁에 있어 달라고 말해보세요.
      </div></section>
      <footer>이용약관 개인정보처리방침 광고 이미지</footer>
    </body></html>
    """
    extracted = NaverKinExtractor().extract(_candidate(), None, html)

    assert extracted is not None
    assert "자살 생각" in extracted.question_body
    assert "응급실" in extracted.answer_body
    assert "광고 이미지" not in extracted.core_text
    assert extracted.body_text == extracted.core_text
