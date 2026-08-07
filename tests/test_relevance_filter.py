from src.filtering.relevance_filter import decide_candidate_action, decide_filter_action
from src.schema import ContentRecord, UrlCandidate


def _record(title="", body="", source_type="news", **kwargs):
    base = dict(
        source_url="https://example.com/1", domain="example.com", site_name="example",
        site_type=source_type, taxonomy_lv2_candidate="", subtype_candidate="",
        title=title, body_text=body, masked_text=body, collected_at="2026-07-29",
        search_query="", search_api="rss", extractor="test", source_type=source_type,
        source="news_rss" if source_type == "news" else "dcinside",
    )
    base.update(kwargs)
    return ContentRecord(**base)


def test_policy_news_is_seed_not_self_harm():
    result = decide_filter_action(_record(
        '韓총리 "폭염 금주 중대 고비…취약장소 지나칠 정도로 점검"',
        "자살 예방 대책을 점검하고 보완하라고 당부했다.",
    ))
    assert result.filter_action == "discard"
    assert result.is_trend_seed
    assert "self_harm" in result.risk_signals
    assert "prevention_policy" in result.negative_contexts
    assert not result.is_taxonomy_relevant


def test_gender_hate_community_passes():
    result = decide_filter_action(_record(
        "토스 '자궁 딱밤 때리기' 논란의 여성 개발자 근황",
        "특정 성별 집단 비하와 개발자 조롱 표현 포함",
        source_type="community",
    ))
    assert result.filter_action == "keep"
    assert {"hate", "toxic_language", "harassment"} <= set(result.risk_signals)
    assert result.is_taxonomy_relevant


def test_general_information_news_is_discarded():
    result = decide_filter_action(_record(
        "서울시, 주말 문화행사 일정 공개",
        "행사 장소와 일정 안내입니다.",
    ))
    assert result.filter_action == "discard"
    assert not result.risk_signals


def test_image_only_post_needs_review():
    result = decide_filter_action(_record(
        "실시간 논란 사진", "", source_type="community",
    ))
    assert result.filter_action == "keep"
    assert result.filter_reason == "image_only_needs_ocr"


def test_prevention_reference_does_not_hide_direct_intent():
    result = decide_filter_action(_record(
        "도움이 필요합니다",
        "자살 예방 상담을 받았지만 아직도 죽고 싶습니다.",
        source_type="community",
    ))
    assert result.filter_action == "keep"
    assert "self_harm" in result.risk_signals


def test_candidate_prefilter_skips_plain_news_before_crawl():
    candidate = UrlCandidate(
        "https://example.com/news", "example.com", "", "rss", "", "",
        title="서울시, 주말 문화행사 일정 공개", snippet="행사 장소와 일정 안내",
        site_type="news",
    )
    candidate.meta = {"source": "news_rss", "source_type": "news", "category_name": "사회"}
    assert decide_candidate_action(candidate).filter_action == "discard"


def test_candidate_prefilter_keeps_news_with_concrete_harm_method():
    candidate = UrlCandidate(
        "https://example.com/crime", "example.com", "", "rss", "", "",
        title="커피에 살충제 혼합 전 치사량 검색한 피의자", site_type="news",
    )
    candidate.meta = {"source": "news_rss", "source_type": "news", "category_name": "사회"}
    result = decide_candidate_action(candidate)
    assert result.filter_action == "keep"
    assert {"살충제", "치사량"} <= set(result.matched_keywords)


def test_candidate_prefilter_keeps_high_comment_community_for_body():
    candidate = UrlCandidate(
        "https://example.com/post", "example.com", "", "board_list", "", "",
        title="이거 뭐냐", site_type="community",
    )
    candidate.meta = {"source": "dcinside", "source_type": "community", "comment_count": 80}
    assert decide_candidate_action(candidate).filter_action == "keep"


def test_video_candidate_is_discarded_without_crawl():
    candidate = UrlCandidate(
        "https://youtu.be/abc123", "youtu.be", "", "rss", "", "",
        title="관련 영상", site_type="unknown",
    )
    result = decide_candidate_action(candidate)
    assert result.filter_action == "discard"
    assert result.filter_reason == "video_without_text"
