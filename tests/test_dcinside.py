from src.common.sources.board import parse_dcinside_trend, parse_doctornow_trend
from src.common.extract.site_parser import DcinsidePostExtractor
from src.common.schema import canonicalize_url
from src.common.site_registry import SiteRegistry


def _list(html=None):
    return parse_dcinside_trend(
        html or LIST_HTML, "https://gall.dcinside.com/board/lists/?id=dcbest",
        _registry(), "latest", "dcbest",
    )


LIST_HTML = """
<table>
  <tr class="ub-content" data-no="1" data-type="icon_notice">
    <td class="gall_num">공지</td>
    <td class="gall_tit"><a href="/board/view/?id=programming&amp;no=1">공지</a></td>
  </tr>
  <tr class="ub-content us-post" data-no="2933928" data-type="icon_btimebest">
    <td class="gall_num">2933928</td>
    <td class="gall_tit ub-word">
      <a href="/board/view/?id=dcbest&amp;no=2933928&amp;page=1">[프갤] 맥 vs windows</a>
      <a class="reply_numbox"><span class="reply_num">[12]</span></a>
    </td>
    <td class="gall_date" title="2026-07-24 10:30:47">10:30</td>
    <td class="gall_count">37</td>
  </tr>
</table>
"""

POST_HTML = """
<div class="gallview_head">
  <span class="title_subject">맥 vs windows</span>
  <span class="gall_writer" data-nick="프갤러"></span>
  <span class="gall_date" title="2026-07-24 10:30:47"></span>
</div>
<div class="write_div">본문에 악플 표현은 그대로 둔다.</div>
"""


def _registry():
    return SiteRegistry.load("configs/site_policy.yaml")


def test_dcinside_list_skips_notice_and_extracts_post_metadata():
    found = _list()
    assert len(found) == 1
    assert found[0].title == "[프갤] 맥 vs windows"
    assert found[0].published_at_hint == "2026-07-24 10:30:47"


def test_dcinside_identity_query_is_preserved_for_dedup():
    first = canonicalize_url("https://gall.dcinside.com/board/view/?id=dcbest&no=1&page=1")
    second = canonicalize_url("https://gall.dcinside.com/board/view/?id=dcbest&no=2&page=1")
    assert first != second


def test_dcinside_post_parser_extracts_body_without_removing_harmful_text():
    content = DcinsidePostExtractor().extract(
        _list()[0], _registry().lookup("gall.dcinside.com"), POST_HTML
    )
    assert content.title == "맥 vs windows"
    assert "악플" in content.body_text


def test_doctornow_list_extracts_card_date():
    html = '''<a href="/content/qna/123"><article>
      <h2>약 복용 상담</h2><p>복용해도 되나요?</p><h3>내과</h3><span>2026.08.13</span>
    </article></a>'''
    found = parse_doctornow_trend(
        html, "https://doctornow.co.kr/content/qna/realtime", _registry(), "medical_qa", "실시간상담"
    )
    assert len(found) == 1
    assert found[0].published_at_hint == "2026-08-13T00:00:00"






def test_image_only_post_is_rejected_as_empty_body():
    """이미지만 있는 글은 본문이 없다. 통과시키면 빈 레코드가 쌓인다(v23에서 이미지 수집 제거)."""
    html = POST_HTML.replace("본문에 악플 표현은 그대로 둔다.", '<img src="/images/long.jpg">')
    content = DcinsidePostExtractor().extract(
        _list()[0], _registry().lookup("gall.dcinside.com"), html
    )
    assert content is None


# ── fetch 실패 진단 ──
def test_empty_body_is_not_reported_as_http_200(monkeypatch):
    """디시는 200 OK + Content-Length 0으로 막는다. http_200으로 적으면 원인을 못 찾는다."""
    from src.common.fetcher import Fetcher

    class _Resp:
        status_code, text, content = 200, "", b""
        headers = {"content-type": "text/html; charset=utf-8"}

    f = Fetcher({"http": {"respect_robots": False, "per_domain_delay": 0}})
    monkeypatch.setattr(f._session, "get", lambda *a, **kw: _Resp())
    assert f.fetch("https://gall.dcinside.com/board/view/?id=dcbest&no=1") is None
    assert f.last_failure == "empty_body"


def test_domain_is_skipped_after_consecutive_failures(monkeypatch):
    """차단된 사이트를 계속 두드리면 차단만 길어진다. 실측에서 114번을 요청했다."""
    from src.common.fetcher import Fetcher

    class _Resp:
        status_code, text, content = 200, "", b""
        headers = {"content-type": "text/html; charset=utf-8"}

    calls = []
    f = Fetcher({"http": {"respect_robots": False, "per_domain_delay": 0,
                          "max_consecutive_failures": 3}})
    monkeypatch.setattr(f._session, "get",
                        lambda *a, **kw: calls.append(a) or _Resp())
    for i in range(10):
        f.fetch(f"https://gall.dcinside.com/board/view/?no={i}")
    assert len(calls) == 3, f"차단 후에도 요청했다: {len(calls)}회"
    assert f.last_failure.startswith("domain_blocked_after")
    # 다른 도메인은 영향받지 않는다
    f.fetch("https://www.yna.co.kr/view/1")
    assert len(calls) == 4


def test_success_resets_the_failure_streak(monkeypatch):
    from src.common.fetcher import Fetcher

    class _Resp:
        def __init__(self, text):
            self.status_code, self.text = 200, text
            self.content = text.encode()
            self.headers = {"content-type": "text/html; charset=utf-8"}

    seq = [_Resp(""), _Resp(""), _Resp("<html>본문</html>"), _Resp(""), _Resp("")]
    f = Fetcher({"http": {"respect_robots": False, "per_domain_delay": 0,
                          "max_consecutive_failures": 3}})
    monkeypatch.setattr(f._session, "get", lambda *a, **kw: seq.pop(0))
    results = [f.fetch(f"https://x.kr/{i}") for i in range(5)]
    assert results[2] and not seq, "성공 후에도 streak이 남아 요청이 끊겼다"
