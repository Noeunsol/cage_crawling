"""Phase 0 — Taxonomy Policy 로더. enabled:true 정책만 반환."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml


# 유해 표현은 보존, PII/credential만 마스킹 (설계서 §12)
_DEFAULT_PRESERVATION = {
    "preserve_harmful_expression": True,
    "mask_pii": True,
    "restrict_actionable_detail": False,
    "mask_credentials": True,
}
# pass 저장 임계 (설계서 §13). review 기준은 pipeline 상수로 고정.
_DEFAULT_THRESHOLDS = {
    "min_taxonomy_fit_score": 0.75,
    "min_harmfulness_score": 0.65,
    "min_seed_source_value_score": 0.60,
}


@dataclass
class Subtype:
    name: str
    description: str = ""
    collection_type: str = "raw_expression"   # raw_expression|qa_consulting|news_case|technical_security
    primary_methods: list[str] = None          # discovery 방식 (없으면 collection_type 기본값)
    preferred_extractors: list[str] = None     # 추출기 우선순위 [ext, ...]
    keywords: list[str] = None
    positive_patterns: list[str] = None
    negative_patterns: list[str] = None
    priority_sites: list[str] = None
    filter_mode: str = None                     # minimal|balanced|strict (mode_by_taxonomy override)
    target_harm_signals: list[str] = None
    preservation_policy: dict = None            # {preserve_harmful_expression, mask_pii, restrict_actionable_detail, mask_credentials}
    thresholds: dict = None                     # {min_taxonomy_fit_score, min_harmfulness_score, min_seed_source_value_score}
    max_pii_risk: float = 1.0
    seed_urls: list[str] = None                 # rss/sitemap/seed_url 방식용

    def __post_init__(self):
        self.primary_methods = self.primary_methods or []
        self.preferred_extractors = self.preferred_extractors or []
        self.keywords = self.keywords or []
        self.positive_patterns = self.positive_patterns or []
        self.negative_patterns = self.negative_patterns or []
        self.priority_sites = self.priority_sites or []
        self.target_harm_signals = self.target_harm_signals or self.keywords.copy()
        self.preservation_policy = {**_DEFAULT_PRESERVATION, **(self.preservation_policy or {})}
        self.thresholds = {**_DEFAULT_THRESHOLDS, **(self.thresholds or {})}
        self.seed_urls = self.seed_urls or []


@dataclass
class Policy:
    taxonomy_lv2: str
    subtypes: list[Subtype]


def load_policies(path: str) -> list[Policy]:
    with open(path, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)

    policies: list[Policy] = []
    for p in data.get("policies", []):
        if not p.get("enabled", False):
            continue
        inherited = {k: p[k] for k in _INHERITED if k in p}
        subtypes = [Subtype(**_subtype_kwargs({**inherited, **s})) for s in p.get("subtypes", [])]
        policies.append(Policy(p["taxonomy_lv2"], subtypes))
    return policies


_ALLOWED = {
    "name", "description", "collection_type", "primary_methods", "preferred_extractors",
    "keywords", "positive_patterns", "negative_patterns", "priority_sites", "filter_mode",
    "target_harm_signals", "preservation_policy", "thresholds", "max_pii_risk", "seed_urls",
}
# taxonomy 레벨에 두면 하위 subtype 전체가 상속 (subtype에서 override 가능)
_INHERITED = {
    "collection_type", "primary_methods", "preferred_extractors", "filter_mode",
    "target_harm_signals", "preservation_policy", "thresholds", "max_pii_risk", "seed_urls",
}


def _subtype_kwargs(s: dict) -> dict:
    """YAML dict에서 Subtype 필드만 추린다 (알 수 없는 키 무시)."""
    return {k: v for k, v in s.items() if k in _ALLOWED}
