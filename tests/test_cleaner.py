"""clean_article_text()의 머리말(네비게이션)/꼬리말(저작권 등) 제거 로직 검증."""

from src.extraction.cleaner import clean_article_text

CFG = {
    "remove_duplicate_paragraphs": True,
    "duplicate_paragraph_min_length": 80,
    "leading_short_line_max_length": 10,
    "trailing_section_markers": ["저작권자", "ⓒ", "무단전재"],
    "trailing_bullet_prefixes": ["▷", "☞", "▲", "▶", "■", "□", "※", "-", "·"],
    "trailing_bullet_min_run": 2,
    "trailing_search_window": 15,
}


def test_strips_leading_short_nav_lines():
    text = "홈\n토픽\n멤버십\n우리나라 같은 경우에는 통신사 개인정보 유출사건 이후로 계속 문제가 되고 있습니다."
    result = clean_article_text(text, CFG)
    assert "홈" not in result.splitlines()
    assert "토픽" not in result.splitlines()
    assert result.startswith("우리나라")


def test_leading_strip_disabled_when_config_is_zero():
    cfg = {**CFG, "leading_short_line_max_length": 0}
    text = "홈\n실제 본문입니다."
    result = clean_article_text(text, cfg)
    assert result.startswith("홈")


def test_leading_strip_stops_after_max_count_even_if_lines_stay_short():
    # 메뉴 노이즈가 아니라 짧은 줄이 계속 이어지는 실제 본문(예: 대화체)이면, 무한정 잘리지
    # 않고 leading_short_line_max_count줄만 지나면 나머지는 그대로 남아야 한다.
    cfg = {**CFG, "leading_short_line_max_count": 2}
    text = "짧다\n또 짧다\n역시 짧음\n넷째 줄도 짧음\n다섯째도 짧음"
    result = clean_article_text(text, cfg)
    assert result.splitlines() == ["역시 짧음", "넷째 줄도 짧음", "다섯째도 짧음"]


def test_strips_trailing_copyright_notice():
    text = "본문 첫 문단입니다.\n본문 둘째 문단.\nⓒ 한경닷컴, 무단전재 및 재배포 금지\nADVERTISEMENT"
    result = clean_article_text(text, CFG)
    assert "무단전재" not in result
    assert "ADVERTISEMENT" not in result
    assert "본문 둘째 문단" in result


def test_marker_far_from_document_end_is_not_treated_as_footer():
    # 저작권 침해 사건을 다루는 기사는 본문 한복판에 "저작권자"가 정당하게 나올 수 있다 —
    # 문서 끝(trailing_search_window) 밖이면 마커가 걸려도 이후 본문을 지우면 안 된다.
    lines = [
        "저작권자 동의 없이 콘텐츠를 무단으로 사용한 사건이 논란이 되고 있다.",
        "피해를 입은 저작권자는 법적 대응을 예고했다.",
    ] + [f"이어지는 본문 설명 {i}번째 줄입니다." for i in range(15)]
    text = "\n".join(lines)
    result = clean_article_text(text, CFG)
    assert "저작권자" in result
    assert "14번째 줄" in result


def test_strips_trailing_marker_even_when_prefixed_by_other_characters():
    # dailian.co.kr처럼 "- Copyrights ⓒ ..." 식으로 마커 앞에 다른 기호가 붙는 사이트가 있다.
    text = "본문 첫 문단입니다.\n- Copyrights ⓒ (주)데일리안, 무단 전재-재배포 금지 -\n관련기사\n☞무관한 기사 제목"
    result = clean_article_text(text, CFG)
    assert "무단 전재" not in result
    assert "관련기사" not in result
    assert "무관한 기사" not in result
    assert "본문 첫 문단" in result


def test_strips_from_reporter_byline_email_regardless_of_outlet():
    # trailing_section_markers에 등록 안 된 매체("sidae.com")라도 "이름 기자 email" 형식이면 잘라낸다.
    text = (
        "본문 첫 문단입니다.\n본문 둘째 문단.\n"
        "강지원 기자 jiwon.kang@sidae.com\n"
        "[시대 주요 뉴스]\n"
        "· 무관한 기사 제목 1\n"
        "· 무관한 기사 제목 2"
    )
    result = clean_article_text(text, CFG)
    assert "jiwon.kang@sidae.com" not in result
    assert "무관한 기사" not in result
    assert "본문 둘째 문단" in result


def test_strips_broadcast_credit_and_report_widget_footer():
    # "영상편집"/"핫 클릭"만 문구로 등록하면 되고, 그 뒤에 붙는 "제보하기" 위젯(▷로 시작하는
    # 줄이 연달아 나옴)은 문구 등록 없이도 불릿 연속 규칙으로 잡혀야 한다.
    cfg = {**CFG, "trailing_section_markers": [*CFG["trailing_section_markers"], "영상편집", "핫 클릭"]}
    text = (
        "본문 첫 문단입니다.\n본문 둘째 문단.\n"
        "(영상편집: 박만기)\n"
        "■ 제보하기\n▷ 카카오톡 : 'KBS제보' 검색, 채널 추가\n"
        "오늘의 핫 클릭"
    )
    result = clean_article_text(text, cfg)
    assert "영상편집" not in result
    assert "제보하기" not in result
    assert "핫 클릭" not in result
    assert "본문 둘째 문단" in result


def test_strips_bullet_widgets_without_registering_their_wording():
    # "관련기사"/"이 기사가 좋으셨다면"은 trailing_section_markers에 없다(CFG 참고) — 그래도
    # 뒤따르는 불릿/화살표 줄이 2개 이상 연속되면 위젯 제목까지 함께 잘려야 한다.
    text = (
        "본문 첫 문단입니다.\n본문 둘째 문단.\n"
        "관련기사\n☞무관한 기사 1\n☞무관한 기사 2\n"
        "이 기사가 좋으셨다면\n- 좋아요 0\n- 응원해요 0\n- 후속 원해요 0"
    )
    result = clean_article_text(text, CFG)
    assert "관련기사" not in result
    assert "무관한 기사" not in result
    assert "본문 둘째 문단" in result


def test_bullet_widget_rule_requires_min_run_so_a_single_dash_line_survives():
    # 본문 중간에 대시로 시작하는 문장이 하나만 있는 경우(인용구 등)까지 지우면 안 된다.
    text = "본문 첫 문단입니다.\n- 이것은 본문 안의 인용구일 뿐입니다.\n본문 이어지는 문단."
    result = clean_article_text(text, CFG)
    assert "인용구" in result
    assert "이어지는 문단" in result


def test_bullet_widget_rule_ignores_explanatory_list_in_middle_of_qna_answer():
    # Q&A/커뮤니티 답변은 본문 중간에도 "- 첫째 ...\n- 둘째 ..." 같은 정상적인 목록을 쓴다.
    # 목록이 문서 끝(trailing_bullet_search_window) 밖에 있으면 위젯으로 보지 않고,
    # 목록 뒤에 이어지는 진짜 본문도 살아있어야 한다.
    lines = [
        "질문에 대한 답변입니다.",
        "다음과 같은 방법이 있습니다:",
        "- 첫째 방법은 이렇습니다.",
        "- 둘째 방법은 이렇습니다.",
    ] + [f"추가로 설명하자면 {i}번째 문단입니다." for i in range(15)]
    text = "\n".join(lines)
    result = clean_article_text(text, CFG)
    assert "첫째 방법" in result
    assert "둘째 방법" in result
    assert "14번째 문단" in result


def test_removes_exact_duplicate_long_paragraphs_only():
    long_paragraph = "이것은 충분히 긴 반복 문단입니다" * 5
    text = f"{long_paragraph}\n짧은 제목\n짧은 제목\n{long_paragraph}"
    result = clean_article_text(text, CFG)
    assert result.count(long_paragraph) == 1
    assert result.count("짧은 제목") == 2  # min_length 미만은 반복돼도 유지
