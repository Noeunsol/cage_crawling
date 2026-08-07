"""모드 공용 헬퍼 — matcher 조립, LLM usage 복사, trend 메타 적용, 공용 상수."""
from __future__ import annotations

import logging

import yaml

from .. import paths
from ..classify.matcher import LLMMatcher, RuleBasedMatcher, TieredMatcher

log = logging.getLogger(__name__)


def _load_llm_cfg(settings: dict) -> dict:
    """configs/llm.yaml(모델/가격) + 호출부 settings.matching.llm 병합. settings가 우선."""
    file_cfg = {}
    try:
        with open(paths.LLM_CONFIG, encoding="utf-8") as f:
            file_cfg = (yaml.safe_load(f) or {}).get("llm", {}) or {}
    except FileNotFoundError:
        pass
    return {**file_cfg, **(settings.get("matching", {}).get("llm", {}) or {})}

# 트렌드 모드 마스킹 정책: 유해 표현 보존 + PII/credential 마스킹 (설계서 §12)
_TREND_PRESERVATION = {"preserve_harmful_expression": True, "mask_pii": True,
                       "restrict_actionable_detail": False, "mask_credentials": True}
# 최종 action → 레거시 filter_status. 최종 상태는 accepted/discard만 허용한다.
_ACTION_TO_STATUS = {"accepted": "pass", "discard": "fail"}


def _build_matcher(settings, auto_save, review_th):
    rule = RuleBasedMatcher(review_threshold=review_th)
    llm_cfg = _load_llm_cfg(settings)
    llm = None
    if llm_cfg.get("enabled"):
        provider = llm_cfg.get("provider", "openai")
        if provider != "openai":
            raise ValueError(f"unsupported LLM provider: {provider}")
        llm = LLMMatcher(model=llm_cfg.get("model", "gpt-4o-mini"),
                         max_chars=llm_cfg.get("max_chars", 4000),
                         provider=provider,
                         pricing=llm_cfg.get("pricing", {}),
                         prompt_path=llm_cfg.get("prompt_path", "prompts/taxonomy_mapping.yaml"))
    return TieredMatcher(rule, llm, auto_save, review_th)


def _apply_trend_meta(rec, meta: dict) -> None:
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


def _copy_llm_usage(rec, cand) -> None:
    """discard 콘텐츠까지 전체 사용량을 집계할 수 있도록 후보에도 usage를 기록한다."""
    for name in (
        "llm_model", "llm_input_tokens", "llm_cached_input_tokens",
        "llm_output_tokens", "llm_total_tokens", "llm_estimated_cost_usd",
    ):
        setattr(cand, name, getattr(rec, name, 0))
