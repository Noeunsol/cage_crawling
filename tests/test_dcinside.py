from src.discovery.board import parse_dcinside_list
from src.extract.site_parser import DcinsidePostExtractor
from src.policy import Subtype
from src.schema import canonicalize_url
from src.site_registry import SiteRegistry
from src.strategy import StrategyTask


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
    task = StrategyTask("T", "S", "raw_expression")
    found = parse_dcinside_list(
        LIST_HTML, "https://gall.dcinside.com/board/lists/?id=dcbest",
        task, Subtype(name="S"), _registry(),
    )
    assert len(found) == 1
    assert found[0].title == "[프갤] 맥 vs windows"
    assert found[0].published_at_hint == "2026-07-24 10:30:47"
    assert found[0].snippet == "comments=12;views=37"


def test_dcinside_identity_query_is_preserved_for_dedup():
    first = canonicalize_url("https://gall.dcinside.com/board/view/?id=dcbest&no=1&page=1")
    second = canonicalize_url("https://gall.dcinside.com/board/view/?id=dcbest&no=2&page=1")
    assert first != second


def test_dcinside_prefilter_rejects_unrelated_title():
    board = {
        "name": "focused",
        "weight": 0.1,
        "min_prefilter_score": 0.25,
        "require_title_signal": True,
        "title_signals": ["악플"],
    }
    task = StrategyTask("T", "Cyberbullying", "raw_expression",
                        target_harm_signals=["악플", "좌표찍기"])
    unrelated = LIST_HTML.replace("[프갤] 맥 vs windows", "오늘 점심 메뉴")
    assert parse_dcinside_list(
        unrelated, "https://gall.dcinside.com/board/lists/?id=dcbest",
        task, Subtype(name="Cyberbullying"), _registry(), board=board,
    ) == []
    related = unrelated.replace("오늘 점심 메뉴", "악플 좌표찍기 피해")
    assert parse_dcinside_list(
        related, "https://gall.dcinside.com/board/lists/?id=dcbest",
        task, Subtype(name="Cyberbullying"), _registry(), board=board,
    )


def test_dcinside_post_parser_extracts_body_without_removing_harmful_text():
    candidate = parse_dcinside_list(
        LIST_HTML, "https://gall.dcinside.com/board/lists/?id=dcbest",
        StrategyTask("T", "S", "raw_expression"), Subtype(name="S"), _registry(),
    )[0]
    content = DcinsidePostExtractor().extract(
        candidate, _registry().lookup("gall.dcinside.com"), POST_HTML
    )
    assert content.title == "맥 vs windows"
    assert "악플" in content.body_text
    assert content.author_hint == "프갤러"


def test_dcinside_fetches_regular_comments_and_replies():
    class FakeFetcher:
        def post_json(self, url, data, referer):
            return {
                "total_cnt": 2,
                "comments": [
                    {"memo": "악플 댓글", "c_no": 0, "del_yn": "N"},
                    {"memo": "reply@example.com", "c_no": 10, "del_yn": "N"},
                ],
            }

    html = POST_HTML.replace(
        '<div class="write_div">',
        '<input id="e_s_n_o" value="token"><input id="_GALLTYPE_" value="G">'
        '<div class="write_div">',
    )
    candidate = parse_dcinside_list(
        LIST_HTML, "https://gall.dcinside.com/board/lists/?id=dcbest",
        StrategyTask("T", "S", "raw_expression"), Subtype(name="S"), _registry(),
    )[0]
    content = DcinsidePostExtractor(FakeFetcher()).extract(
        candidate, _registry().lookup("gall.dcinside.com"), html
    )
    assert content.comments == ["[댓글] 악플 댓글", "[대댓글] reply@example.com"]


def test_dcinside_keeps_comments_for_image_only_post():
    class FakeFetcher:
        def post_json(self, url, data, referer):
            return {"total_cnt": 1, "comments": [
                {"memo": "이미지 글의 댓글", "c_no": 0, "del_yn": "N"}
            ]}

    html = POST_HTML.replace(
        '<div class="write_div">본문에 악플 표현은 그대로 둔다.</div>',
        '<input id="e_s_n_o" value="token"><img src="x.jpg">',
    )
    candidate = parse_dcinside_list(
        LIST_HTML, "https://gall.dcinside.com/board/lists/?id=dcbest",
        StrategyTask("T", "S", "raw_expression"), Subtype(name="S"), _registry(),
    )[0]
    content = DcinsidePostExtractor(FakeFetcher()).extract(
        candidate, _registry().lookup("gall.dcinside.com"), html
    )
    assert content.body_text == ""
    assert content.comments == ["[댓글] 이미지 글의 댓글"]


def test_dcinside_extracts_body_image_urls():
    html = POST_HTML.replace(
        "본문에 악플 표현은 그대로 둔다.",
        '<img data-original="/images/long.jpg"><img src="data:image/png;base64,x">',
    )
    candidate = parse_dcinside_list(
        LIST_HTML, "https://gall.dcinside.com/board/lists/?id=dcbest",
        StrategyTask("T", "S", "raw_expression"), Subtype(name="S"), _registry(),
    )[0]
    content = DcinsidePostExtractor().extract(
        candidate, _registry().lookup("gall.dcinside.com"), html
    )
    assert content.image_urls == ["https://gall.dcinside.com/images/long.jpg"]
