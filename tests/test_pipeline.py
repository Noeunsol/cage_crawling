"""end-to-end 검증 (오프라인). 실제 파서를 fixture HTML로 구동한다. assert 기반."""
import sqlite3

from src import fetcher, pipeline
from src.keyword_discovery import search
from src.storage.dedup import hamming, simhash
from src.mask import mask_pii
from src.classify.matcher import RuleBasedMatcher, TieredMatcher
from src.policy import Subtype
from src.schema import ContentRecord

# ── fixture HTML (PII + Cyberbullying 키워드 포함) ──
_PII = "제보는 010-1234-5678 또는 report@example.com, 주민번호 900101-1234567."

NAVER_KIN = """<html><head><title>온라인 악플 대응 질문</title></head><body>
<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{
"question":"온라인 커뮤니티에서 악플과 좌표찍기 때문에 너무 힘든데 어떻게 대응해야 하나요 며칠째 특정 게시판에서 여러 사람이 몰려와 댓글로 계속 괴롭힘을 당하고 있습니다 캡처는 계속 모으고 있는데 고소가 가능한지 궁금합니다",
"answer":"우선 게시글과 댓글 작성자 정보를 시간 순서대로 캡처해 증거를 확보하세요 명예훼손과 모욕죄로 고소가 가능하며 커뮤니티 자체 신고 기능으로 게시글 삭제와 이용자 제재도 요청할 수 있습니다 피해가 지속되면 경찰청 사이버수사대에 신고할 수 있고 필요하면 법률 상담을 병행하는 것이 좋습니다",
"date":"2026-07-08","contact":"%s"}}}</script></body></html>""" % _PII

NEWS = """<html><head><meta property="og:title" content="온라인 커뮤니티 악플 논란 확산">
<meta property="article:published_time" content="2026-07-09T10:00:00"></head><body>
<article><p>최근 한 온라인 커뮤니티에서 악플과 좌표찍기 문제가 다시 불거졌다. 특정 이용자를 겨냥한 댓글이 반복적으로 달렸고 조리돌림으로 번지면서 논란이 커졌다.</p>
<p>피해자는 관련 게시글과 댓글을 시간 순서대로 캡처해 모아 명예훼손과 모욕 혐의로 고소를 준비 중이라고 밝혔다. 커뮤니티 운영진은 신고가 접수된 게시글을 일부 삭제했지만 이미 여러 채널로 확산된 뒤라 완전한 회수는 어려운 상황이다.</p>
<p>전문가들은 익명 공간에서의 집단적 괴롭힘이 어떻게 빠르게 번지는지, 그리고 피해자가 겪는 심리적 고통이 얼마나 큰지를 지적한다. 온라인 커뮤니티 문화 전반에 대한 성찰이 필요하다는 목소리도 나온다.</p>
<p>비슷한 피해 사례가 늘면서 커뮤니티 차원의 신고 및 차단 정책 강화가 필요하다는 목소리가 커지고 있다. 관계 당국도 온라인 괴롭힘에 대한 대응 방안을 검토하고 있다. %s</p></article></body></html>""" % _PII

# 댓글은 수집하지 않으므로 본문만으로 community min_body_chars를 넘겨야 한다.
COMMUNITY = """<html><head><title>악플 좌표찍기 실화냐</title></head><body>
<div class="content">온라인 커뮤니티에서 악플과 좌표찍기 당했다 진짜 너무하다.
특정 이용자를 겨냥한 조리돌림이 며칠째 이어지고 있고 캡처를 모아 고소를 준비 중이다.
운영진에 신고했지만 게시글이 이미 여러 채널로 퍼진 뒤라 회수가 안 된다.
여러 명이 몰려와 좌표를 찍고 악플을 다는 건 명백한 사이버불링이라고 본다.
피해자가 겪는 정신적 고통이 큰데도 온라인폭력은 처벌까지 가는 경우가 드물다.
운영진은 게시글을 삭제하고 반복 가해 이용자를 제재해야 한다 %s</div></body></html>""" % _PII

DC_LIST = """<table><tr class="ub-content us-post" data-no="2933928" data-type="icon_txt">
<td class="gall_num">2933928</td>
<td class="gall_tit"><a href="/board/view/?id=dcbest&amp;no=2933928">[원갤] 악플 좌표찍기 실화냐</a></td>
<td class="gall_date" title="2026-07-08 10:00:00">10:00</td>
<td class="gall_count">37</td></tr></table>"""


def _fake_fetch(self, url):
    if "/board/lists" in url:
        return DC_LIST
    if "kin.naver.com" in url:
        return NAVER_KIN
    if "news.naver.com" in url:
        return NEWS
    return COMMUNITY


def _run(tmp_path, monkeypatch, **kw):
    monkeypatch.setitem(search._CLIENTS, "serpapi", search.MockTavily())   # 외부 API 미호출
    monkeypatch.setattr(fetcher.Fetcher, "fetch", _fake_fetch)             # 네트워크 미호출
    monkeypatch.setattr(fetcher.Fetcher, "post_json", lambda *args: None)
    db = tmp_path / "content.db"
    report = pipeline.run(
        db_path=str(db), report_path=str(tmp_path / "r.json"),
        csv_path=str(tmp_path / "c.csv"), after="2025-01-01", before="2027-01-01",
        max_queries=10000, **kw)   # 테스트는 mock이라 검색 상한 해제(전체 커버리지)
    return db, report


def test_end_to_end_real_parsers(tmp_path, monkeypatch):
    db, report = _run(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)

    # (a) Toxic Language pass 레코드 + 최소 필드
    rows = conn.execute(
        "SELECT source_url, title, body_text FROM content_records "
        "WHERE taxonomy_lv2='1_A_Toxic_Language' AND filter_status='pass'"
    ).fetchall()
    assert rows, "Toxic Language pass 레코드가 최소 1건"
    for url, title, body in rows:
        assert url and title and body

    # (b) 실제 정적 파서 여러 종류가 fixture로 동작
    extractors = {r[0] for r in conn.execute("SELECT DISTINCT extractor FROM content_records")}
    assert "trafilatura" in extractors         # 뉴스 본문+날짜
    assert "dcinside" in extractors or "site_parser_community" in extractors

    # (g) body_text는 정제 본문이며 원문 내용을 유지한다.
    r = conn.execute("SELECT body_text, raw_text FROM content_records LIMIT 1").fetchone()
    assert "010-1234-5678" in r[0]
    assert "010-1234-5678" in r[1]

    # (g) CSV에는 raw_text 미포함
    with open(tmp_path / "c.csv", encoding="utf-8-sig") as f:
        header = f.readline()
    assert "raw_text" not in header and "body_text" in header

    assert report["stored_records"] >= 1
    conn.close()


def test_dry_run_and_reset(tmp_path, monkeypatch):
    # dry-run: DB 미생성, 프리뷰만
    monkeypatch.setitem(search._CLIENTS, "serpapi", search.MockTavily())
    rep = pipeline.run(db_path=str(tmp_path / "none.db"),
                       report_path=str(tmp_path / "r.json"), csv_path=str(tmp_path / "c.csv"),
                       after="2025-01-01", before="2027-01-01", dry_run=True)
    assert rep["dry_run"] and not (tmp_path / "none.db").exists()

    # reset-db: 두 번째 실행이 신규 컬럼 마이그레이션/재생성으로 정상 동작
    _run(tmp_path, monkeypatch)
    _, report2 = _run(tmp_path, monkeypatch, reset_db=True)
    assert report2["stored_records"] >= 1


def test_pii_masking():
    masked = mask_pii("연락 010-1234-5678, a.b@example.com, 900101-1234567")
    assert "[PHONE]" in masked and "[EMAIL]" in masked and "[RRN]" in masked
    assert "010-1234-5678" not in masked and "900101-1234567" not in masked


def test_simhash_near_dup():
    base = ("온라인 커뮤니티에서 특정 이용자를 겨냥한 악플과 좌표찍기가 반복되며 조리돌림으로 번졌다 "
            "피해자는 게시글과 댓글을 캡처해 명예훼손으로 고소를 준비 중이라고 밝혔다 "
            "운영진은 신고된 게시글을 삭제했지만 이미 여러 채널로 확산된 뒤였다")
    near = base.replace("반복되며", "계속 반복되며").replace("삭제했지만", "일부 삭제했지만")
    far = "오늘 점심으로 김치찌개를 먹었는데 맛집이라 사람이 많았고 대기 시간이 길었다 다음엔 예약하고 가야겠다"
    # near-dup은 무관 문서보다 훨씬 가까움 (SimHash 판별력)
    assert hamming(simhash(base), simhash(near)) < hamming(simhash(base), simhash(far))
    assert hamming(simhash(base), simhash(base)) == 0


def test_llm_parse_fallback():
    """LLM이 비JSON 반환 시 rule 결과로 fallback + llm_parse_error 기록."""
    class BadLLM:
        def match(self, *a, **k):
            return None   # 파싱 실패를 흉내 (LLMMatcher는 실패 시 None 반환)

    subtype = Subtype(name="Cyberbullying", keywords=["악플", "좌표찍기"],
                      positive_patterns=["온라인 커뮤니티", "댓글"])
    rec = ContentRecord(source_url="u", domain="d", site_name="s", site_type="community",
                        taxonomy_lv2_candidate="Toxic Language", subtype_candidate="Cyberbullying",
                        title="악플 논란", body_text="", masked_text="온라인 커뮤니티 악플 댓글",
                        collected_at="2026-07-15", search_query="q", search_api="serpapi", extractor="x",
                        value_score=0.9)
    tiered = TieredMatcher(RuleBasedMatcher(review_threshold=0.5), BadLLM(), auto_save=0.8, review_th=0.5)
    result = tiered.match("Toxic Language", subtype, rec)
    assert result.reason.startswith("rule:")               # rule 결과 사용
    assert "llm_parse_error" in (rec.llm_escalation_reason or "")
