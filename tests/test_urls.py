"""normalize_url()의 \\uXXXX 복구가 쿼리 문자열에만 적용되는지 검증 (경로/호스트 오염 방지)."""

from src.utils.urls import normalize_url


def test_leaked_encoded_unicode_escape_in_query_is_decoded():
    # 실제 버그 형태(2026-08-26 실측, mediatoday.co.kr): 키 뒤에 %5cu003d가 붙고 값 끝에
    # 엉뚱한 '='가 남는다. 쿼리 문자열 안에서는 여전히 정상적으로 고쳐져야 한다.
    url = "https://mediatoday.co.kr/news.html?idxno%5cu003d333363="
    assert normalize_url(url) == "https://mediatoday.co.kr/news.html?idxno=333363"


def test_literal_backslash_u_sequence_in_path_is_left_untouched():
    # 경로(쿼리 문자열 앞부분)에 우연히 \\uXXXX 모양의 문자열이 있어도 다른 문자로 바뀌면 안 된다
    # — 바뀌면 원래 리소스가 아닌 엉뚱한 URL을 가져오게 된다.
    url = "https://example.com/post/\\u003d-review"
    assert normalize_url(url) == "https://example.com/post/\\u003d-review"
