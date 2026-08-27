"""clean_article_text()의 머리말(네비게이션)/꼬리말(저작권 등) 제거 로직 검증."""

from src.extraction.cleaner import clean_article_text

CFG = {
    "remove_duplicate_paragraphs": True,
    "duplicate_paragraph_min_length": 80,
    "leading_short_line_max_length": 10,
    "trailing_section_markers": ["저작권자", "ⓒ", "무단전재"],
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


def test_removes_exact_duplicate_long_paragraphs_only():
    long_paragraph = "이것은 충분히 긴 반복 문단입니다" * 5
    text = f"{long_paragraph}\n짧은 제목\n짧은 제목\n{long_paragraph}"
    result = clean_article_text(text, CFG)
    assert result.count(long_paragraph) == 1
    assert result.count("짧은 제목") == 2  # min_length 미만은 반복돼도 유지
