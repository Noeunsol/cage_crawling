"""본문 추출: snippet이 아닌 실제 정제 본문만 저장 가능하고, 실패/중복이 올바르게 분류된다."""

import requests

from src.extraction import duplicates, parser_registry
from src.extraction.fetcher import FetchError, fetch
from src.extraction.general_extractor import ExtractedContent, ExtractionError, extract
from src.storage import database
from src.storage.repositories import contents as contents_repo
from src.utils.text import compute_content_hash
from src.utils.urls import normalize_url

SAMPLE_HTML = """
<html><head><title>메타 제목</title></head>
<body>
<nav>메뉴 메뉴 메뉴 홈 로그인</nav>
<article>
<h1>진짜 제목입니다</h1>
<p>2026-01-01</p>
<p>이것은 본문 첫 문단입니다. 충분히 긴 텍스트를 넣어서 trafilatura가 본문으로 인식하게 합니다.
자살 예방과 관련된 상담 내용을 다루는 커뮤니티 게시글 예시입니다. 실제로는 더 긴 내용이 있을 수 있습니다.</p>
<p>본문 두번째 문단입니다. 계속해서 이야기가 이어집니다. 광고나 배너와는 무관한 순수 텍스트 콘텐츠입니다.</p>
</article>
<aside>광고 배너 클릭하세요</aside>
<footer>Copyright 2026</footer>
</body></html>
"""

EXTRACTION_CFG = {"fetch": {"user_agent": "test-agent", "timeout_seconds": 5}}
RETRY_POLICY = {
    "reasons": {
        "timeout": {"retry_mode": "immediate"},
        "temporary_http_error": {"retry_mode": "immediate"},
        "access_denied": {"retry_mode": "never"},
        "not_found": {"retry_mode": "never"},
    }
}


# ---------------------------------------------------------------- URL 정규화
def test_normalize_url_strips_tracking_params_fragment_and_sorts_query():
    url = "HTTP://Example.com/a/b/?utm_source=x&b=2&a=1#section"
    assert normalize_url(url) == "http://example.com/a/b?a=1&b=2"


def test_normalize_url_treats_trailing_slash_as_equivalent():
    assert normalize_url("https://example.com/a/") == normalize_url("https://example.com/a")


def test_normalize_url_repairs_leaked_js_unicode_escape():
    # 일부 사이트가 href의 "="을 =로 이스케이프해놓고 디코딩을 안 해서, 검색 API가 그
    # raw 문자열을 그대로 돌려주는 경우가 있다 (2026-08-26 실측: mediatoday.co.kr).
    leaked = "https://www.mediatoday.co.kr/news/articleView.html?idxno" + chr(92) + "u003d333363"
    assert normalize_url(leaked) == normalize_url(
        "https://www.mediatoday.co.kr/news/articleView.html?idxno=333363"
    )


def test_normalize_url_repairs_percent_encoded_js_unicode_escape():
    leaked = "https://www.mediatoday.co.kr/news/articleView.html?idxno%5Cu003d333363="
    assert normalize_url(leaked) == normalize_url(
        "https://www.mediatoday.co.kr/news/articleView.html?idxno=333363"
    )


# ---------------------------------------------------------------- 콘텐츠 해시
def test_compute_content_hash_ignores_whitespace_and_case_differences():
    h1 = compute_content_hash("제목", "본문 내용입니다.")
    h2 = compute_content_hash("제목", "본문   내용입니다.\n\n\n")
    h3 = compute_content_hash("제목", "완전히 다른 본문입니다.")
    assert h1 == h2
    assert h1 != h3


# ---------------------------------------------------------------- fetcher
class _FakeResponse:
    def __init__(self, status_code, text="", url="https://example.com/final", headers=None):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = headers or {}
        self.apparent_encoding = "utf-8"
        self.encoding = "utf-8"


class _FakeEncodingResponse:
    """requests.Response처럼 .encoding에 따라 .text 디코딩 결과가 달라지는 fake."""

    def __init__(self, content: bytes, headers, apparent_encoding="utf-8"):
        self.status_code = 200
        self.url = "https://example.com/final"
        self._content = content
        self.headers = headers
        self.encoding = "ISO-8859-1"  # requests가 charset 없을 때 기본으로 단정하는 값
        self.apparent_encoding = apparent_encoding

    @property
    def text(self):
        return self._content.decode(self.encoding, errors="replace")


def test_fetch_success(monkeypatch):
    monkeypatch.setattr(
        "requests.get", lambda *a, **kw: _FakeResponse(200, text="<html>ok</html>")
    )
    result = fetch("https://example.com/a", EXTRACTION_CFG, RETRY_POLICY)
    assert result.html == "<html>ok</html>"
    assert result.final_url == "https://example.com/final"


def test_fetch_corrects_encoding_when_charset_not_declared(monkeypatch):
    # lawlogos.com 재현: Content-Type에 charset이 없으면 requests가 ISO-8859-1로 잘못 단정한다.
    korean_html = "<html><title>타이틀</title></html>"
    fake = _FakeEncodingResponse(
        korean_html.encode("utf-8"), headers={"Content-Type": "text/html"}, apparent_encoding="utf-8"
    )
    monkeypatch.setattr("requests.get", lambda *a, **kw: fake)
    result = fetch("https://example.com/a", EXTRACTION_CFG, RETRY_POLICY)
    assert result.html == korean_html


def test_fetch_trusts_declared_charset(monkeypatch):
    # charset이 명시되어 있으면 apparent_encoding과 달라도 건드리지 않는다.
    fake = _FakeEncodingResponse(
        b"<html>ok</html>", headers={"Content-Type": "text/html; charset=UTF-8"}, apparent_encoding="euc-kr"
    )
    monkeypatch.setattr("requests.get", lambda *a, **kw: fake)
    result = fetch("https://example.com/a", EXTRACTION_CFG, RETRY_POLICY)
    assert result.html == "<html>ok</html>"


def test_fetch_timeout_is_retryable(monkeypatch):
    def raise_timeout(*a, **kw):
        raise requests.exceptions.Timeout()
    monkeypatch.setattr("requests.get", raise_timeout)

    try:
        fetch("https://example.com/a", EXTRACTION_CFG, RETRY_POLICY)
        assert False, "FetchError가 발생해야 합니다"
    except FetchError as e:
        assert e.reason == "timeout"
        assert e.retryable is True


def test_fetch_403_is_access_denied_not_retryable(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **kw: _FakeResponse(403))
    try:
        fetch("https://example.com/a", EXTRACTION_CFG, RETRY_POLICY)
        assert False
    except FetchError as e:
        assert e.reason == "access_denied"
        assert e.retryable is False


def test_fetch_404_is_not_found(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **kw: _FakeResponse(404))
    try:
        fetch("https://example.com/a", EXTRACTION_CFG, RETRY_POLICY)
        assert False
    except FetchError as e:
        assert e.reason == "not_found"


def test_fetch_500_is_temporary_http_error_retryable(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **kw: _FakeResponse(500))
    try:
        fetch("https://example.com/a", EXTRACTION_CFG, RETRY_POLICY)
        assert False
    except FetchError as e:
        assert e.reason == "temporary_http_error"
        assert e.retryable is True


def test_fetch_429_is_retryable_like_5xx(monkeypatch):
    # 429(rate limit)는 4xx지만 500대처럼 서버의 일시적 상태라 재시도 대상이어야 한다.
    # 예전엔 "status >= 400"에 걸려 무조건 retryable=False로 영구 폐기됐다.
    monkeypatch.setattr("requests.get", lambda *a, **kw: _FakeResponse(429))
    try:
        fetch("https://example.com/a", EXTRACTION_CFG, RETRY_POLICY)
        assert False
    except FetchError as e:
        assert e.reason == "temporary_http_error"
        assert e.retryable is True


def test_fetch_400_stays_not_retryable(monkeypatch):
    # 429를 제외한 나머지 4xx(요청 자체가 잘못된 경우)는 여전히 재시도해도 소용없다.
    monkeypatch.setattr("requests.get", lambda *a, **kw: _FakeResponse(400))
    try:
        fetch("https://example.com/a", EXTRACTION_CFG, RETRY_POLICY)
        assert False
    except FetchError as e:
        assert e.reason == "temporary_http_error"
        assert e.retryable is False


# ---------------------------------------------------------------- general_extractor
def test_extract_handles_backslashes_in_body_without_json_decode_error():
    # 예전엔 trafilatura output_format="json" + json.loads(raw)를 썼는데, 본문에 백슬래시가
    # 섞이면 trafilatura가 만든 JSON 문자열 자체가 깨져 JSONDecodeError가 났다(2026-08-26 실측).
    # bare_extraction()으로 dict를 바로 받으면 이 JSON 왕복이 없어 문제가 안 생긴다.
    html = """
    <html><head><title>메타 제목</title></head>
    <body><article>
    <h1>진짜 제목입니다</h1>
    <p>윈도 경로 예시는 C:\\Users\\test\\file.txt 처럼 백슬래시가 들어갈 수 있습니다.
    정규식이나 이스케이프 문자(\\n, \\t)도 본문에 그대로 등장할 수 있는 충분히 긴 문단입니다.</p>
    </article></body></html>
    """
    result = extract(html, url="https://example.com/a", min_content_length=10)
    assert "C:\\Users\\test\\file.txt" in result.content


def test_extract_returns_clean_body_without_nav_ads_footer():
    result = extract(SAMPLE_HTML, url="https://example.com/a", min_content_length=10)
    assert result.title == "진짜 제목입니다"
    assert "메뉴" not in result.content
    assert "광고 배너" not in result.content
    assert "Copyright" not in result.content
    assert "본문 첫 문단" in result.content
    assert result.published_date == "2026-01-01"


def test_extract_defaults_to_comments_off_and_precision_on(monkeypatch):
    captured = {}

    def fake_bare_extraction(html, **kwargs):
        captured.update(kwargs)
        return None  # 본문 자체는 안 봐도 되니 실패로 짧게 끝낸다

    monkeypatch.setattr("trafilatura.bare_extraction", fake_bare_extraction)
    try:
        extract(SAMPLE_HTML, url="https://example.com/a", min_content_length=10)
    except ExtractionError:
        pass

    assert captured["include_comments"] is False
    assert captured["favor_precision"] is True


def test_extract_respects_extraction_cfg_overrides(monkeypatch):
    captured = {}

    def fake_bare_extraction(html, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr("trafilatura.bare_extraction", fake_bare_extraction)
    cfg = {"trafilatura": {"include_comments": True, "favor_precision": False}}
    try:
        extract(SAMPLE_HTML, url="https://example.com/a", min_content_length=10, extraction_cfg=cfg)
    except ExtractionError:
        pass

    assert captured["include_comments"] is True
    assert captured["favor_precision"] is False


def test_extract_removes_author_profile_and_repeated_paragraphs(monkeypatch):
    article = "\n".join([
        "사건 발생 경위와 피해 내용을 설명하는 충분히 긴 첫 번째 본문 문단입니다.",
        "수사기관이 확인한 후속 상황을 설명하는 충분히 긴 두 번째 본문 문단입니다.",
        "사건 발생 경위와 피해 내용을 설명하는 충분히 긴 첫 번째 본문 문단입니다.",
        "[글_홍길동 기자]",
        "필자 소개_",
        "- 어느 기관 전문위원",
    ])

    class FakeDocument:
        def as_dict(self):
            return {"title": "사건 기사", "text": article, "date": "2026-01-01"}

    monkeypatch.setattr("trafilatura.bare_extraction", lambda *a, **kw: FakeDocument())
    cfg = {"cleaning": {
        "remove_duplicate_paragraphs": True,
        "duplicate_paragraph_min_length": 20,
        "trailing_section_markers": ["[글_", "필자 소개"],
    }}

    result = extract("<html></html>", "https://example.com/a", 10, cfg)

    assert result.content.count("사건 발생 경위") == 1
    assert "수사기관이 확인한" in result.content
    assert "홍길동 기자" not in result.content
    assert "전문위원" not in result.content


def test_extract_fails_when_body_too_short():
    tiny_html = "<html><head><title>t</title></head><body><p>짧음</p></body></html>"
    try:
        extract(tiny_html, url="https://example.com/a", min_content_length=1000)
        assert False, "ExtractionError가 발생해야 합니다"
    except ExtractionError as e:
        assert e.reason == "extraction_too_short"


# ---------------------------------------------------------------- parser_registry
def test_parser_registry_defaults_to_general_extractor():
    parser = parser_registry.get_parser("unknown-domain.com")
    result = parser(SAMPLE_HTML, "https://unknown-domain.com/a", 10)
    assert isinstance(result, ExtractedContent)


def test_parser_registry_uses_registered_domain_parser():
    def fake_parser(html, url, min_len, extraction_cfg=None):
        return ExtractedContent(title="커스텀", content="커스텀 파서 결과", published_date=None)

    parser_registry.register("special.com", fake_parser)
    parser = parser_registry.get_parser("special.com")
    assert parser("<html></html>", "https://special.com/a", 1).title == "커스텀"


def test_get_parser_falls_back_to_general_extractor_when_dedicated_parser_fails():
    def broken_parser(html, url, min_len, extraction_cfg=None):
        raise ExtractionError("extraction_empty")

    parser_registry.register("broken.com", broken_parser)
    parser = parser_registry.get_parser("broken.com")
    result = parser(SAMPLE_HTML, "https://broken.com/a", 10)
    assert result.title == "진짜 제목입니다"


def test_extract_falls_back_to_title_tag_when_trafilatura_finds_no_title():
    # 본문만 있고 trafilatura가 title로 인식할 h1/og:title이 전혀 없는 페이지.
    html = """
    <html><head><title>title 태그뿐인 제목</title></head>
    <body><p>본문만 있고 트라필라투라가 제목으로 인식할 요소가 전혀 없는 페이지입니다.
    충분히 긴 텍스트를 넣어서 본문 길이 기준을 통과시킵니다. 광고나 배너와 무관한 순수 텍스트입니다.</p></body>
    </html>
    """
    result = extract(html, url="https://example.com/a", min_content_length=10)
    assert result.title == "title 태그뿐인 제목"


# ---------------------------------------------------------------- 중복 체크
def test_url_and_content_duplicate_checks(tmp_path):
    conn = database.connect(tmp_path / "test.db")
    content_id, _ = contents_repo.upsert_content(
        conn, title="제목", content="본문", published_date=None,
        canonical_url="https://example.com/a", source_name=None,
        source_domain="example.com", source_category=None,
        status="accepted", content_hash=compute_content_hash("제목", "본문"),
    )

    url_dup = duplicates.check_url_duplicate(conn, "https://example.com/a")
    assert url_dup.is_duplicate is True
    assert url_dup.reason == "same_url"
    assert url_dup.existing_content_id == content_id

    content_dup = duplicates.check_content_duplicate(conn, compute_content_hash("제목", "본문"))
    assert content_dup.is_duplicate is True
    assert content_dup.reason == "same_content_hash"

    assert duplicates.check_url_duplicate(conn, "https://example.com/b").is_duplicate is False
    assert duplicates.check_content_duplicate(conn, "다른-해시").is_duplicate is False
