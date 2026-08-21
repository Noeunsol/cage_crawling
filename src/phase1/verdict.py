"""1차 trend 판정·저장 마무리.

trend는 본문을 LLM에 보내 taxonomy를 정하고, 그 점수를 로컬 임계값으로 다시 판정한다.
2차(phase2)는 이 규칙을 쓰지 않는다 — 저장 판정은 phase2/acceptance.py가 따로 한다.
"""
from __future__ import annotations

# 최종 action → 레거시 filter_status. 최종 상태는 accepted/discard만 허용한다.
_ACTION_TO_STATUS = {"accepted": "pass", "discard": "fail"}


def classification_action(match, rec, settings: dict) -> str:
    """LLM 점수를 로컬 기준으로 재판정한다: accepted | discard."""
    if not match.is_relevant:
        return "discard"
    m = settings.get("matching", {})
    confidence = match.confidence
    fit = rec.taxonomy_fit_score or 0
    concrete = rec.concrete_context_score or 0
    korea = rec.korea_relevance_score or 0
    if korea < float(m.get("min_korea_relevance", 0.3)):
        return "discard"
    if rec.filter_status not in {"pass", "review"}:
        return "discard"
    if (confidence >= float(m.get("accepted_confidence", 0.75))
            and fit >= float(m.get("accepted_taxonomy_fit", 0.70))
            and concrete >= float(m.get("accepted_concrete_context", 0.50))):
        return "accepted"
    return "discard"


def apply_trend_meta(rec, meta: dict) -> None:
    """discovery가 목록에서 읽은 부가 메타(게시판·조회수·버킷)를 레코드로 옮긴다."""
    rec.source = meta.get("source", "")
    rec.source_type = meta.get("source_type", "")
    rec.board_name = meta.get("board_name", "")
    rec.category_name = meta.get("category_name", "")
    rec.view_count = meta.get("view_count")
    rec.comment_count = meta.get("comment_count")
    rec.like_count = meta.get("like_count")
    rec.is_trending = bool(meta.get("is_trending", False))
    rec.collection_type = meta.get("bucket") or rec.collection_type   # 스펙: 버킷(trending/latest/rss)
    rec.crawl_status = "success"


def finalize(store, cand, rec, save_raw_text, store_content: bool) -> None:
    """최종 판정과 후보 감사 로그를 저장한다. discard 본문은 저장하지 않는다."""
    rec.filter_status = _ACTION_TO_STATUS.get(rec.action, "fail")
    rec.canonical_url = cand.canonical_url or cand.source_url
    if store_content:
        if not save_raw_text:
            rec.raw_text = ""
        store.save_content(rec)
    cand.filter_action = rec.filter_action
    cand.status, cand.filter_reason = rec.action, rec.filter_reason
    store.save_candidate(cand)
    store.log_filter(cand.source_url, "relevance_filter", rec.filter_status, rec.filter_reason, "", "")
