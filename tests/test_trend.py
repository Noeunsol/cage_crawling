"""트렌드 수집 모드 검증 (오프라인, assert 기반).

(a) 샘플링 버킷 배분 (b) 1차 basic_filter (c) 2차 위험신호 후보/override
(d) 단일 taxonomy 매핑(닫힌 어휘) + 스코어링 (e) RSS pubDate 윈도우 + end-to-end.
"""
import sqlite3
import datetime as dt

import pytest
import yaml

from src import fetcher, pipeline
from src.pipelines import trend as trend_pipeline
from src.discovery import rss
from src.discovery.board import parse_dcinside_trend
from src.classify.matcher import (LLMMatcher, RuleBasedMatcher, build_taxonomy_index, confidence_bucket,
                         risk_score_of, trend_score_of)
from src.policy import load_policies
from src.filtering.quality import QualityFilter, basic_filter
from src.pipelines._trend_util import _allocate_buckets
from src.filtering.risk_signals import detect_signals, primary_override
from src.schema import ContentRecord, MatchResult, UrlCandidate
from src.site_registry import SiteRegistry
from src.pipelines._trend_util import _parse_datetime, _sample_candidates_by_time
from src.pipelines.taxonomy_adjudication import _trend_classification_action

TAXO = "configs/taxonomy.yaml"


def test_taxonomy_canonical_file_has_19_lv2_and_67_types():
    policies = load_policies(TAXO)
    assert len(policies) == 19
    assert sum(len(policy.subtypes) for policy in policies) == 67
    assert all(
        policy.taxonomy_lv1
        and policy.taxonomy_lv2_name
        and policy.definition
        and policy.description
        for policy in policies
    )
    assert all(subtype.description for policy in policies for subtype in policy.subtypes)


def test_taxonomy_loader_rejects_duplicate_yaml_keys(tmp_path):
    path = tmp_path / "duplicate.yaml"
    path.write_text("policies: []\npolicies: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="중복 YAML 키: policies"):
        load_policies(str(path))

LIST_HTML = """
<table>
  <tr class="ub-content" data-no="1" data-type="icon_notice">
    <td class="gall_num">공지</td>
    <td class="gall_tit"><a href="/board/view/?id=dcbest&amp;no=1">공지</a></td>
  </tr>
  <tr class="ub-content us-post" data-no="2933928" data-type="icon_btimebest">
    <td class="gall_num">2933928</td>
    <td class="gall_tit ub-word">
      <a href="/board/view/?id=dcbest&amp;no=2933928&amp;page=1">신상 털고 좌표 찍자</a>
      <a class="reply_numbox"><span class="reply_num">[12]</span></a>
    </td>
    <td class="gall_date" title="2026-07-24 10:30:47">10:30</td>
    <td class="gall_count">37</td>
  </tr>
</table>
"""

# 욕설(강함) + 개인정보 + 선동 → 단일매핑은 4_I로 override 되어야 한다.
POST_HTML = """
<div class="gallview_head">
  <span class="title_subject">신상 털고 좌표 찍자</span>
  <span class="gall_writer" data-nick="갤러"></span>
  <span class="gall_date" title="2026-07-24 10:30:47"></span>
</div>
<div class="write_div">이 병신 새끼 신상 다 털어서 전화번호 010-1234-5678 이랑 집주소까지 공개하자
좌표 찍고 다같이 몰려가서 조리돌림 하자 이런 글이 커뮤니티에 계속 올라오는데 너무 심각한 문제다
캡처 다 해놨다 아주 개새끼가 따로 없다</div>
"""


def _registry():
    return SiteRegistry.load("configs/site_policy.yaml")


def _rec(**kw):
    base = dict(source_url="u", domain="gall.dcinside.com", site_name="dcinside",
                site_type="community", taxonomy_lv2_candidate="", subtype_candidate="",
                title="", body_text="", collected_at="2026-07-27", search_query="q",
                search_api="board_list", extractor="dcinside")
    base.update(kw)
    return ContentRecord(**base)


# ── (a) 샘플링 버킷 배분 ──
def test_allocate_buckets_splits_by_ratio():
    cands = []
    for bucket, n in (("trending", 50), ("latest", 50), ("high_comment", 50), ("high_risk_board", 50)):
        for _ in range(n):
            c = UrlCandidate("u", "d", "", "board_list", "", "")
            c.meta = {"bucket": bucket}
            cands.append(c)
    ratios = {"trending": 0.50, "latest": 0.25, "high_comment": 0.15, "high_risk_board": 0.10}
    picked = pipeline._allocate_buckets(cands, 100, ratios)
    assert len(picked) == 100
    from collections import Counter
    dist = Counter(c.meta["bucket"] for c in picked)
    assert dist["trending"] == 50 and dist["latest"] == 25 and dist["high_comment"] == 15


def test_candidate_pool_keeps_replacements_for_later_discards():
    titles = [
        "오늘 전국의 평범한 날씨와 주말 기온을 자세히 알아봅니다",
        "특정인의 신상과 전화번호를 찾아서 모두 공개하자는 게시물",
        "봄철에 즐기기 좋은 평범한 점심 메뉴와 맛집 정보를 소개합니다",
        "외부 사이트 해킹 방법과 취약점 공격 절차를 자세히 공유합니다",
    ]
    cands = []
    for i, title in enumerate(titles):
        cand = UrlCandidate(f"https://example.com/{i}", "example.com", "", "rss", "", "",
                            title=title)
        cand.meta = {"source": "news_rss", "source_type": "news"}
        cands.append(cand)

    selected, seen = [], set()
    pipeline._append_unique_candidates(cands, selected, seen, scan_cap=3)

    assert len(selected) == 3


def test_candidates_are_interleaved_by_source():
    def candidate(url):
        return UrlCandidate(url, "example.com", "", "rss", "", "")

    out = pipeline._round_robin_candidates({
        "dcinside": [candidate("dc1"), candidate("dc2")],
        "news_rss": [candidate("news1"), candidate("news2")],
    })
    assert [c.source_url for c in out] == ["dc1", "news1", "dc2", "news2"]


def _dated_candidates(counts):
    timezone = dt.datetime.now().astimezone().tzinfo
    today = dt.datetime.now(timezone).date()
    out = []
    for day_offset, count in enumerate(counts):
        day = today - dt.timedelta(days=day_offset)
        for i in range(count):
            hour = (i % 6) * 4 + (i // 6) % 4
            stamp = dt.datetime.combine(day, dt.time(hour, i % 60), tzinfo=timezone)
            cand = UrlCandidate(
                f"https://example.com/{day_offset}/{i}", "example.com", "", "board_list", "", "",
                title="위험 신호 후보", published_at_hint=stamp.isoformat(), site_type="community",
            )
            cand.meta = {
                "bucket": "random_trend", "comment_count": i % 20,
                "like_count": i % 7, "view_count": i * 10,
            }
            out.append(cand)
    return out, timezone


def test_time_sampling_selects_200_per_day_and_spreads_hours():
    candidates, timezone = _dated_candidates([240, 240, 240])
    picked = _sample_candidates_by_time(
        candidates, 3, 200, {"random_trend": 1.0}, timezone,
        time_bucket_hours=4, engagement_ratio=0.3, absolute_max=1000,
    )
    by_day, by_slot = {}, {}
    for cand in picked:
        published = _parse_datetime(cand.published_at_hint, timezone)
        by_day[published.date()] = by_day.get(published.date(), 0) + 1
        key = (published.date(), published.hour // 4)
        by_slot[key] = by_slot.get(key, 0) + 1
    assert len(picked) == 600 and sorted(by_day.values()) == [200, 200, 200]
    assert all(33 <= count <= 34 for count in by_slot.values())


def test_time_sampling_redistributes_short_day_quota():
    candidates, timezone = _dated_candidates([300, 300, 50])
    picked = _sample_candidates_by_time(
        candidates, 3, 200, {"random_trend": 1.0}, timezone,
        absolute_max=1000,
    )
    oldest = dt.datetime.now(timezone).date() - dt.timedelta(days=2)
    assert len(picked) == 600
    assert sum(
        _parse_datetime(c.published_at_hint, timezone).date() == oldest for c in picked
    ) == 50


def test_parse_dcinside_trend_tags_meta():
    cands = parse_dcinside_trend(LIST_HTML, "https://gall.dcinside.com/board/lists/?id=dcbest",
                                 _registry(), "trending", "실시간베스트")
    assert len(cands) == 1                      # 공지 제외
    m = cands[0].meta
    assert m["source"] == "dcinside" and m["bucket"] == "trending" and m["is_trending"]
    assert m["comment_count"] == 12 and m["view_count"] == 37


# ── (b) 1차 basic_filter ──
def test_basic_filter_rules():
    assert basic_filter(_rec(title="ㅋㅋ", masked_text="ㅇㅇ")).status == "fail"          # 잡담/짧음
    assert basic_filter(_rec(masked_text="https://a.b/c")).reason == "link_only"
    assert basic_filter(_rec(masked_text="짧음")).status == "fail"                        # too_short
    ok = _rec(title="제목", masked_text="본문이 충분히 길고 의미가 있는 한국어 문장입니다 정말로요")
    assert basic_filter(ok).status == "pass"


# ── (c) 위험신호 후보 + disambiguation override ──
def test_risk_signal_and_override():
    only = detect_signals("씨발 병신아 진짜")
    assert only == {"toxic_language"} and primary_override(only) is None
    mixed = detect_signals("이 병신 신상 다 털고 전화번호 공개하자")
    assert primary_override(mixed) == "4_I_Privacy_Infringement"


# ── (d) 단일 taxonomy 매핑(닫힌 어휘) + 스코어링 ──
def test_classify_single_mapping_closed_vocab():
    policies = load_policies(TAXO)
    valid, _ = build_taxonomy_index(policies)
    matcher = RuleBasedMatcher()

    pure = _rec(masked_text="씨발 진짜 병신같은 새끼 개짜증나네 꺼져라")
    r1 = matcher.classify(pure, policies)
    assert (r1.taxonomy_lv2, r1.subtype) in valid                  # 닫힌 어휘
    assert r1.taxonomy_lv2 == "1_A_Toxic_Language" and r1.subtype == "profanity_and_insults"

    mixed = _rec(masked_text="이 병신 새끼 신상 다 털어서 전화번호 공개하자 아주 개새끼네")
    r2 = matcher.classify(mixed, policies)
    assert (r2.taxonomy_lv2, r2.subtype) in valid
    assert r2.taxonomy_lv2 == "4_I_Privacy_Infringement"           # override

    # 성혐오 글 + 과장된 "죽여"(violence 신호) → violence로 새지 않고 gender로 매핑돼야 한다
    gender = _rec(masked_text="한녀충들 지랄발광 ㅋㅋ 병신년들 설리 죽여놓고 한남탓 여성혐오 오지네")
    r3 = matcher.classify(gender, policies)
    assert r3.taxonomy_lv2 == "2_F_Bias_and_Hate" and r3.subtype == "gender"


def test_all_taxonomies_reachable():
    """키워드가 명확한 전형 예문은 기대 lv2로 매핑돼야 한다(구조·disambiguation 회귀 가드).

    주의: 깨끗한 입력 기준. 실제 잡음 섞인 글의 정확도는 별도 평가셋으로 측정해야 한다.
    """
    policies = load_policies(TAXO)
    matcher = RuleBasedMatcher()
    probes = [
        ("1_A_Toxic_Language", "씨발 이 병신새끼 진짜 개짜증나네 꺼져 지랄하지말고"),
        ("1_B_Sexual_Content", "야동 음란물 링크 여기 있음 성인물 무료로 봄"),
        ("1_C_Self_Harm", "진짜 죽고싶다 자살 방법 검색중 자해도 계속함"),
        ("1_D_Child_Exploitation", "아청물 아동음란물 미성년성착취물 공유 그루밍"),
        ("2_E_Discrimination", "저 회사는 대놓고 채용차별 배제하고 불이익"),
        ("2_F_Bias_and_Hate", "한녀충들 여성혐오 김치녀 페미 다 문제"),
        ("3_G_Misinformation_and_Disinformation", "이거 찌라시 카더라 루머 폭로 확인되지않은 소문"),
        ("3_H_Prohibited_Advisory", "주식리딩 코인추천 원금보장 리딩방"),
        ("4_I_Privacy_Infringement", "저새끼 신상 다 털었다 전화번호유출 주소공개 얼굴공개"),
        ("4_J_Public_Sensitive_Info_Leakage", "회사 내부문서 대외비 기밀문서 유출자료"),
        ("4_K_Intellectual_Property_Infringement", "토렌트 불법다운로드 웹하드 자막공유 무료다운"),
        ("5_L_Illegal_Activity", "보이스피싱 대포통장 불법도박 사설토토 마약"),
        ("5_M_Violence_and_Extremism", "흉기난동 폭행 살인 방화 묻지마범죄"),
        ("5_N_Encouraging_Unethical_Actions", "대리시험 과제대행 논문표절 가스라이팅 심리조작"),
        ("6_O_CBRNE", "폭탄제조 사제폭탄 폭발물제조 화학무기 사린"),
        ("6_P_Cybersecurity", "랜섬웨어 악성코드 해킹 디도스 키로거"),
    ]
    wrong = [(exp, matcher.classify(_rec(masked_text=t), policies).taxonomy_lv2)
             for exp, t in probes
             if matcher.classify(_rec(masked_text=t), policies).taxonomy_lv2 != exp]
    assert not wrong, f"오분류: {wrong}"


def test_scoring_functions():
    assert risk_score_of({"privacy", "toxic_language"}, 0.5) == 5
    assert risk_score_of({"toxic_language"}, 0.7) == 3
    assert risk_score_of(set(), None) == 1
    assert trend_score_of(1, 120, True) == 5
    assert trend_score_of(30, 0, False) == 1
    assert confidence_bucket(0.8) == 4 and confidence_bucket(0.1) == 1
    # gate 어휘로 risk_score가 정확히 매겨지는지 (illegal_activity/rumor_or_misinformation 등)
    assert risk_score_of({"illegal_activity"}, 0.1) == 5
    assert risk_score_of({"rumor_or_misinformation"}, 0.1) == 4


# ── (e) RSS pubDate 윈도우 필터 ──
def test_news_window_filter(monkeypatch):
    # 날짜를 실행 시점 기준으로 만든다. 하드코딩하면 윈도우를 지나는 순간부터 테스트가 깨진다.
    recent = (dt.datetime.now().astimezone() - dt.timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S %z")

    def fake_items(url):
        return [
            {"link": "https://news.example/recent", "title": "최신", "published_at": recent},
            {"link": "https://news.example/old", "title": "오래됨", "published_at": "Tue, 01 Jan 2019 09:00:00 +0900"},
        ]
    monkeypatch.setattr(rss, "feed_items", fake_items)
    feeds = [{"press": "예시", "category": "사회", "url": "http://x"}]
    out = rss.discover_news_trend(feeds, _registry(), exclude_older_than_days=14)
    links = [c.source_url for c in out]
    assert "https://news.example/recent" in links
    assert "https://news.example/old" not in links      # 윈도우 초과 제외
    assert out[0].meta["source"] == "news_rss"


def test_news_category_allocation_balances_and_deduplicates():
    def cand(url, category):
        c = UrlCandidate(url, "example.com", "", "rss", "", "")
        c.meta = {"category_name": category}
        return c

    candidates = [
        cand("https://example.com/p1", "정치"),
        cand("https://example.com/p2", "정치"),
        cand("https://example.com/s1", "사회"),
        cand("https://example.com/s1", "사회"),
    ]
    # 뉴스는 bucket이 없고 category_name이 주제 축이다(_topic_of가 흡수).
    out = _allocate_buckets(candidates, 2, {"정치": 0.5, "사회": 0.5})
    assert len(out) == 2
    assert {c.meta["category_name"] for c in out} == {"정치", "사회"}


def test_llm_classify_records_harmfulness_and_korean_context(monkeypatch):
    policies = load_policies(TAXO)
    valid, lines = build_taxonomy_index(policies)
    matcher = LLMMatcher()
    monkeypatch.setattr(matcher, "_complete_json", lambda *_: {
        "is_taxonomy_relevant": True,
        "filter_status": "pass",
        "category": "suicide",
        "is_harmful": True,
        "harmfulness_score": 0.9,
        "contains_korean_context": True,
        "korea_relevance_score": 0.8,
        "concrete_context_score": 0.9,
        "confidence": 0.95,
        "reason": "직접적인 자살 의도",
        "evidence_spans": ["죽고 싶다"],
        "fail_reason": None,
    })
    rec = _rec(masked_text="죽고 싶다")
    result = matcher.classify(rec, policies, valid, lines)
    assert result.is_relevant and result.taxonomy_lv2 == "1_C_Self_Harm"
    assert result.taxonomy_lv1 == "Toxicity Harms"
    assert rec.harmfulness_score == 0.9
    assert rec.concrete_context_score == 0.9 and rec.evidence_spans == ["죽고 싶다"]
    assert rec.contains_korean_context is True and rec.korea_relevance_score == 0.8


def test_review_level_score_is_finalized_as_accepted():
    rec = _rec()
    rec.filter_status = "review"
    rec.taxonomy_fit_score = 0.55
    rec.harmfulness_score = 0.50
    rec.concrete_context_score = 0.40
    rec.korea_relevance_score = 0.50
    match = MatchResult(True, "1_A_Toxic_Language", "profanity_and_insults", 0.55, "test")
    settings = {"matching": {
        "accepted_confidence": 0.50, "accepted_taxonomy_fit": 0.50,
        "accepted_harmfulness": 0.45, "accepted_concrete_context": 0.30,
        "min_korea_relevance": 0.30,
    }}
    assert _trend_classification_action(match, rec, settings) == "accepted"


def test_low_harmfulness_does_not_block_concrete_taxonomy_match():
    rec = _rec()
    rec.filter_status = "pass"
    rec.taxonomy_fit_score = 0.70
    rec.harmfulness_score = 0.05
    rec.concrete_context_score = 0.60
    rec.korea_relevance_score = 0.80
    match = MatchResult(True, "5_L_Illegal_Activity", "fraudulent_schemes_and_deception", 0.70, "test")
    settings = {"matching": {
        "accepted_confidence": 0.50, "accepted_taxonomy_fit": 0.50,
        "accepted_concrete_context": 0.30, "min_korea_relevance": 0.30,
    }}
    assert _trend_classification_action(match, rec, settings) == "accepted"


def test_llm_type_derives_path_and_repairs_relevance_contradiction(monkeypatch):
    policies = load_policies(TAXO)
    matcher = LLMMatcher()
    monkeypatch.setattr(matcher, "_complete_json", lambda *_: {
        "is_taxonomy_relevant": False,
        "filter_status": "fail",
        "category": "gender",
        "is_harmful": True,
        "harmfulness_score": 0.8,
        "contains_korean_context": True,
        "korea_relevance_score": 1.0,
        "concrete_context_score": 0.8,
        "confidence": 0.8,
        "reason": "성별 집단 비하",
        "evidence_spans": ["한녀"],
        "fail_reason": None,
    })
    rec = _rec(masked_text="성별 집단을 비하하는 글")
    result = matcher.classify(rec, policies)
    assert not result.is_relevant
    assert (result.taxonomy_lv1, result.taxonomy_lv2, result.subtype) == (
        "Unfair Representation", "2_F_Bias_and_Hate", "gender",
    )
    assert rec.filter_status == "fail"
    assert result.reason.startswith("llm_classify")


def test_llm_allows_lv2_without_type(monkeypatch):
    policies = load_policies(TAXO)
    matcher = LLMMatcher()
    monkeypatch.setattr(matcher, "_complete_json", lambda *_: {
        "is_taxonomy_relevant": True,
        "filter_status": "pass",
        "category": "-",
        "taxonomy_lv2": "5_L_Illegal_Activity",
        "is_harmful": True,
        "harmfulness_score": 0.8,
        "contains_korean_context": True,
        "korea_relevance_score": 1.0,
        "concrete_context_score": 0.9,
        "confidence": 0.8,
        "reason": "범죄 수법이 구체적임",
        "evidence_spans": ["사기 수법"],
        "fail_reason": None,
    })
    rec = _rec(masked_text="사기 수법을 설명한 글")
    result = matcher.classify(rec, policies)
    assert result.is_relevant
    assert result.taxonomy_lv2 == "5_L_Illegal_Activity"
    assert result.taxonomy_lv1 == "Malicious Use"
    assert result.subtype == "-"
    assert rec.filter_status == "pass"


def test_llm_input_uses_title_and_body_only():
    matcher = LLMMatcher()
    rec = _rec(title="제목", masked_text="본문", masked_comments=["댓글 비밀 문맥"])
    user_text = matcher._user_text(rec)
    assert "제목" in user_text and "본문" in user_text
    assert "댓글" not in user_text and "비밀 문맥" not in user_text


def test_llm_usage_and_cost_are_recorded():
    matcher = LLMMatcher(
        model="gpt-4o-mini", provider="openai",
        pricing={"input_per_million_usd": 0.15,
                 "cached_input_per_million_usd": 0.075,
                 "output_per_million_usd": 0.60},
    )
    matcher.last_usage = {"input": 1000, "cached": 200, "output": 500, "total": 1500}
    rec = _rec()
    matcher._record_usage(rec)
    assert rec.llm_total_tokens == 1500 and rec.llm_cached_input_tokens == 200
    assert rec.llm_estimated_cost_usd == 0.000435


def test_llm_rubric_uses_definitions_not_keyword_rules():
    policies = load_policies(TAXO)
    _, lines = build_taxonomy_index(policies)
    rubric = "\n".join(lines)
    assert len(lines) == 19
    assert "Definition:" in rubric and "Description:" in rubric and "types:" in rubric
    assert "cyberbullying_and_harassment: 온라인·디지털 환경에서" in rubric
    assert "positive=" not in rubric and "negative=" not in rubric and "keywords=" not in rubric


def test_policy_without_types_uses_lv2_as_single_type(tmp_path):
    cfg = tmp_path / "taxonomy.yaml"
    cfg.write_text(yaml.safe_dump({"policies": [{
        "enabled": True,
        "taxonomy_lv1": "Example Harms",
        "taxonomy_lv2": "X_Example",
        "taxonomy_lv2_name": "Example",
        "definition": "예시 정의",
        "description": "예시 설명",
    }]}, allow_unicode=True), encoding="utf-8")
    policy = load_policies(str(cfg))[0]
    assert len(policy.subtypes) == 1
    assert policy.subtypes[0].name == "Example"
    assert policy.subtypes[0].description == "예시 설명"


# ── (f) 본문 링크 추출 + follow ──
def test_extract_links_skips_media_and_dedup():
    links = pipeline._extract_links(
        "본문 https://m.dcinside.com/board/x/1 사진 https://cdn.x/a.JPG "
        "영상 https://youtu.be/abc 끝",
        ["댓글 https://n.news.naver.com/article/1", "중복 https://m.dcinside.com/board/x/1"])
    assert ("https://m.dcinside.com/board/x/1", "body") in links
    assert ("https://n.news.naver.com/article/1", "comment") in links
    assert ("https://youtu.be/abc", "body") in links
    assert not any(url.lower().endswith(".jpg") for url, _ in links)
    assert sum(url == "https://m.dcinside.com/board/x/1" for url, _ in links) == 1


# ── end-to-end (dcinside만, 오프라인) ──
def _fake_fetch(self, url):
    return LIST_HTML if "/board/lists" in url else POST_HTML


def test_run_trend_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher.Fetcher, "fetch", _fake_fetch)
    monkeypatch.setattr(fetcher.Fetcher, "post_json", lambda *a, **k: None)
    trend_cfg = tmp_path / "trend.yaml"
    trend_cfg.write_text(yaml.safe_dump({
        "collection": {"exclude_older_than_days": 3650},
        "target_by_source": {"dcinside": 5},
        "sampling_ratio": {"trending": 1.0},
        "sources": {
            "dcinside": {"enabled": True, "max_pages": 1,
                         "galleries": [{"id": "dcbest", "name": "실베", "bucket": "trending"}]},
            "news_rss": {"enabled": False},
        },
    }, allow_unicode=True), encoding="utf-8")

    db = tmp_path / "trend.db"
    report = pipeline.run_trend(
        trend_config=str(trend_cfg), taxonomy_config=TAXO,
        db_path=str(db), report_path=str(tmp_path / "r.json"), csv_path=str(tmp_path / "c.csv"),
    )
    assert report["stored_records"] == 0
    assert report["collection_window"]["lookback_days"] == 3650
    assert report["run_id"]

    conn = sqlite3.connect(db)
    discarded = conn.execute(
        "SELECT COUNT(*) FROM url_candidates WHERE status='trend_discard'"
    ).fetchone()[0]
    assert discarded >= 1  # LLM 미사용/실패는 pending 저장 없이 discard
    run_rows = conn.execute(
        "SELECT DISTINCT run_id, collection_phase FROM url_candidates"
    ).fetchall()
    assert run_rows == [(report["run_id"], 1)]
    conn.close()


def test_run_trend_allows_doctornow_without_dcinside(tmp_path, monkeypatch):
    monkeypatch.setattr(trend_pipeline, "discover_doctornow_trend", lambda *args, **kwargs: [])
    cfg = tmp_path / "trend.yaml"
    cfg.write_text(yaml.safe_dump({
        "collection": {"lookback_days": 1},
        "sources": {
            "dcinside": {"enabled": False},
            "doctornow": {"enabled": True, "boards": [{}]},
            "news_rss": {"enabled": False},
        },
    }, allow_unicode=True), encoding="utf-8")
    report = pipeline.run_trend(
        trend_config=str(cfg), taxonomy_config=TAXO, dry_run=True,
        report_path=str(tmp_path / "r.json"), csv_path=str(tmp_path / "c.csv"),
    )
    assert report["collection_targets"]["doctornow"]["available"] == 0


def test_run_trend_skips_previously_processed_url_before_body_fetch(tmp_path, monkeypatch):
    body_fetches = 0

    def counted_fetch(self, url):
        nonlocal body_fetches
        if "/board/lists" not in url:
            body_fetches += 1
        return LIST_HTML if "/board/lists" in url else POST_HTML

    monkeypatch.setattr(fetcher.Fetcher, "fetch", counted_fetch)
    monkeypatch.setattr(fetcher.Fetcher, "post_json", lambda *a, **k: None)
    cfg = tmp_path / "trend.yaml"
    cfg.write_text(yaml.safe_dump({
        "collection": {"exclude_older_than_days": 3650},
        "sources": {
            "dcinside": {"enabled": True, "max_pages": 1,
                         "galleries": [{"id": "dcbest", "name": "실베", "bucket": "trending"}]},
            "news_rss": {"enabled": False},
        },
    }, allow_unicode=True), encoding="utf-8")
    kwargs = dict(
        trend_config=str(cfg), taxonomy_config=TAXO, db_path=str(tmp_path / "trend.db"),
        report_path=str(tmp_path / "r.json"), csv_path=str(tmp_path / "c.csv"),
    )
    pipeline.run_trend(**kwargs)
    first = body_fetches
    report = pipeline.run_trend(**kwargs)
    assert first > 0 and body_fetches == first
    assert report["collection_targets"]["dcinside"]["already_processed"] == 1


# ── quality: site_type별 길이 하한 (커뮤니티 글은 원래 짧다) ──
def _q_rec(body, site_type, date="2026-08-12"):
    rec = ContentRecord(
        source_url="https://gall.dcinside.com/board/view/?id=dcbest&no=1", domain="gall.dcinside.com",
        site_name="dcinside", site_type=site_type, taxonomy_lv2_candidate="1_A_Toxic_Language",
        subtype_candidate="", title="신상 박제 논란", body_text=body, masked_text=body,
        collected_at="2026-08-12", search_query="q", search_api="x", extractor="x")
    rec.published_at = date
    return rec


def test_quality_min_chars_by_site_type():
    """extraction이 통과시킨 짧은 커뮤니티 글을 quality가 되버리면 fetch 비용만 낭비된다."""
    settings = yaml.safe_load(open("configs/crawler_settings.yaml", encoding="utf-8"))
    # 80자 이상 200자 미만 — extraction(community=80)은 통과, 구 전역 하한(200)에서는 탈락하던 구간
    short = ("커뮤니티에서 신상이 박제돼 피해를 보고 있다는 글이 계속 올라온다 "
             "고소가 되는지 묻는 댓글도 달렸고 캡처는 다 모아뒀다고 한다 "
             "운영진에 신고했지만 아직 아무 조치가 없어서 답답한 상황이다")
    assert 80 <= len(short) < 200
    assert QualityFilter(settings).check(_q_rec(short, "community")).status == "pass"
    # 같은 길이라도 뉴스는 전역 하한(200)을 그대로 적용받는다
    assert QualityFilter(settings).check(_q_rec(short, "news")).reason.startswith("too_short")
