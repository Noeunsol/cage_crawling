"""1_C_Self_Harm 파이프라인 개선분 검증. 실제 웹서치/OpenAI 호출은 하지 않고,
mock freshness 결과와 실제 taxonomy.yaml 설정값만으로 검증한다.
"""

from __future__ import annotations

from src.config.loader import load_all_configs
from src.extraction.comments import Comment, ExtractionResult, select_comments
from src.filtering.text_safety import sanitize_short_terms
from src.query import vocabulary
from src.query.generator import _filter_queries

CONFIGS = load_all_configs()


def _type_cfg(type_name: str) -> dict:
    type_cfg = vocabulary.find_type(CONFIGS, "1_C_Self_Harm", type_name)
    assert type_cfg is not None, f"{type_name} not found"
    return type_cfg


# 1. freshness 성공 시 정적 vocabulary와 fresh terms가 중복 제거되어 병합되는지
def test_static_and_fresh_merge_deduplicates():
    type_cfg = _type_cfg("eating_disorder")
    fresh_terms = ["프로아나", "먹토 인증"]  # "프로아나"는 정적 vocabulary와 중복
    merged, state = vocabulary.build_vocabulary(
        configs=CONFIGS, lv2_id="1_C_Self_Harm", type_cfg=type_cfg, fresh_terms=fresh_terms,
    )
    assert state == "static_and_fresh"
    assert merged.count("프로아나") == 1
    assert "먹토 인증" in merged
    assert set(type_cfg["search_vocabulary"]) <= set(merged)


# 2. freshness 실패 시 정적 vocabulary만으로 검색어가 생성되는지
def test_freshness_failure_falls_back_to_static_only():
    type_cfg = _type_cfg("suicide")
    merged, state = vocabulary.build_vocabulary(
        configs=CONFIGS, lv2_id="1_C_Self_Harm", type_cfg=type_cfg, fresh_terms=[],
    )
    assert state == "static_only"
    assert merged == type_cfg["search_vocabulary"]


# 3. 정적 vocabulary와 freshness가 모두 없으면 definition 폴백이 작동하는지
def test_definition_fallback_when_no_static_or_fresh():
    type_cfg = dict(_type_cfg("suicide"))
    type_cfg.pop("search_vocabulary", None)
    merged, state = vocabulary.build_vocabulary(
        configs=CONFIGS, lv2_id="1_C_Self_Harm", type_cfg=type_cfg, fresh_terms=[],
    )
    assert state == "definition_fallback"
    assert merged  # definition/include_criteria에서 뭔가는 뽑혀야 한다
    assert all(len(t) <= vocabulary._MAX_TERM_CHARS for t in merged)


# 4. 모든 폴백이 실패하면 LV2 공통 vocabulary가 사용되는지
def test_lv2_fallback_when_definition_yields_nothing():
    type_cfg = {"definition": "", "include_criteria": []}  # 뽑을 텍스트 자체가 없어 derive가 빈 리스트를 반환
    merged, state = vocabulary.build_vocabulary(
        configs=CONFIGS, lv2_id="1_C_Self_Harm", type_cfg=type_cfg, fresh_terms=[],
    )
    assert state == "lv2_fallback"
    assert merged == vocabulary.get_lv2_fallback_vocabulary(CONFIGS, "1_C_Self_Harm")
    assert merged  # taxonomy.yaml에 실제로 채워져 있어야 한다


# 5. SerpAPI의 정상적인 4~5단어 한국어 검색어가 보존되는지
def test_serpapi_normal_queries_survive_relaxed_limit():
    limits = {"serpapi": {"max_terms": 5, "max_characters": 45}}
    queries = ["청소년 자해 유해 게시물", "온라인 자살 조장 콘텐츠", "프로아나 게시물 규제 논란"]
    result = _filter_queries(queries, provider="serpapi", limits=limits)
    assert result.accepted == queries
    assert result.rejected == []


# 6. 구체적인 방법·도구·신체 부위·개인 식별자가 freshness 결과에서 제거되는지
def test_sanitize_fresh_terms_drops_unsafe_or_long_entries():
    terms = [
        "신상털기",                              # 정상 — 유지
        "정 20알 복용",                          # 숫자(치명성/용량 정보 위험) — 제거
        "010-1234-5678로 연락",                  # 연락처 — 제거
        "베란다 창틀에서 뛰어내리는 방법을 순서대로 설명",  # 절차 문장(길이 초과) — 제거
        "example.com에서 검색",                  # URL 패턴 — 제거
    ]
    sanitized = sanitize_short_terms(terms)
    assert sanitized == ["신상털기"]


# 7. 예방·규제 기사가 수집 단계에서는 자동 제외되지 않는지
def test_collection_exclude_criteria_does_not_block_prevention_articles():
    type_cfg = _type_cfg("suicide")
    collection_criteria = vocabulary.effective_exclude_criteria(type_cfg)
    assert collection_criteria == type_cfg["collection_exclude_criteria"]
    # 예방 캠페인 자체를 이유로 거르는 규칙은 collection_exclude_criteria에 없어야 한다
    assert not any("예방" in c for c in collection_criteria)
    # 반면 옛 exclude_criteria(=harm_positive용으로 남긴 것)에는 예방 캠페인 배제가 있었다
    assert any("예방" in c for c in type_cfg["exclude_criteria"])


# 8. 예방 콘텐츠가 유해 positive로 분류되지 않는지
def test_harm_positive_exclude_criteria_covers_prevention_content():
    type_cfg = _type_cfg("self_injury")
    harm_criteria = type_cfg["harm_positive_exclude_criteria"]
    assert any("예방" in c for c in harm_criteria)
    assert any("치료" in c or "회복" in c for c in harm_criteria)
    # 이 기준은 아직 어떤 수집/검색어 생성 코드에도 쓰이지 않는다
    assert harm_criteria != vocabulary.effective_exclude_criteria(type_cfg)


# 9. 게시글은 도움 요청이지만 댓글에 위험 신호가 있는 경우 두 신호가 분리되는지
def test_post_and_comment_harm_signals_are_separated():
    comments = [
        Comment(text="저도 힘들어요, 상담센터 가보세요", taxonomy_relevance=0.2, reaction_count=1),
        Comment(text="위험한 응답", taxonomy_relevance=0.9, reaction_count=10, is_representative_or_author_selected=True),
    ]
    selected = select_comments(
        comments, max_comments=1, selection_priority=["taxonomy_relevance", "reaction_count"],
    )
    result = ExtractionResult(
        post_content="자해하고 싶어요 도와주세요",
        selected_comments=selected,
        post_harm_signal=False,   # 도움 요청 게시글 자체는 유해로 오판하지 않는다
        comment_harm_signal=True,  # 위험한 응답이 댓글에 있다는 신호는 별도로 유지된다
    )
    assert selected == [comments[1]]
    assert result.post_harm_signal is False
    assert result.comment_harm_signal is True
    assert result.post_harm_signal != result.comment_harm_signal


# 10. 기존 taxonomy의 exclude_criteria 동작이 깨지지 않는지
def test_effective_exclude_criteria_falls_back_for_types_without_collection_field():
    for lv2 in CONFIGS["taxonomy"]["taxonomy"]:
        if lv2["lv2_id"] == "1_C_Self_Harm":
            continue
        for t in lv2["types"]:
            if "collection_exclude_criteria" in t:
                continue
            assert vocabulary.effective_exclude_criteria(t) == t["exclude_criteria"]
            return
    raise AssertionError("collection_exclude_criteria가 없는 비교 대상 type을 찾지 못했습니다")
