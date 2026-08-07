"""탈락/보류/2차 레코드 저장 마무리 (store 쓰기 idiom)."""
from __future__ import annotations

from .stages import _ACTION_TO_STATUS


def _finalize(store, cand, rec, save_raw_text, store_content: bool) -> None:
    """최종 판정과 후보 감사 로그를 저장한다. discard 본문은 저장하지 않는다."""
    rec.filter_status = _ACTION_TO_STATUS.get(rec.action, "fail")
    rec.canonical_url = cand.canonical_url or cand.source_url
    if store_content:
        if not save_raw_text:
            rec.raw_text, rec.raw_comments = "", None
        store.save_content(rec)
    cand.filter_action = rec.filter_action
    cand.status, cand.filter_reason = f"trend_{rec.action}", rec.filter_reason
    store.save_candidate(cand)
    store.log_filter(cand.source_url, "relevance_filter", rec.filter_status, rec.filter_reason, "", "")


def _phase2_store(store, cand, rec, save_raw_text, target_lv2) -> None:
    rec.canonical_url = rec.canonical_url or cand.canonical_url or cand.source_url
    if not save_raw_text:
        rec.raw_text, rec.raw_comments = "", None
    store.save_content(rec)
    cand.status = f"trend_{rec.action}"
    cand.filter_reason = rec.filter_reason
    store.save_candidate(cand)
    store.log_filter(cand.source_url, "phase2_classify", rec.filter_status,
                     rec.filter_reason or rec.action, rec.taxonomy_lv2 or target_lv2, rec.category or "")
