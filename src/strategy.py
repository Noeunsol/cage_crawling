"""수집 전략 결정 — subtype을 collection_type 기반 실행 task로 펼치고, 예산(Budget)을 관리한다.

collection_type(설계서 §7)이 수집 성격을 정하고, 그에 맞는 discovery method 체인을 태운다.
subtype 하나 = 실행 task 하나(단일 plan). primary_methods 미지정 시 collection_type 기본 체인 사용.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .policy import Subtype

# collection_type → 기본 discovery method 체인 (subtype.primary_methods 없을 때, 설계서 §8)
_DEFAULT_METHODS = {
    "raw_expression": ["board_list", "serpapi_site"],
    "qa_consulting": ["serpapi_site", "tavily"],
    "news_case": ["rss", "sitemap", "tavily", "serpapi_site"],
    "technical_security": ["exa", "github", "serpapi_site"],
}


@dataclass
class StrategyTask:
    taxonomy_lv2: str
    subtype: str
    collection_type: str
    discovery_methods: list[str] = field(default_factory=list)
    preferred_extractors: list[str] = field(default_factory=list)
    preservation_policy: dict = field(default_factory=dict)
    thresholds: dict = field(default_factory=dict)
    target_harm_signals: list[str] = field(default_factory=list)
    seed_urls: list[str] = field(default_factory=list)


class StrategyRouter:
    """subtype 하나 → collection_type 하나의 실행 task (단일 plan)."""
    def build_tasks(self, taxonomy_lv2: str, subtype: Subtype) -> list[StrategyTask]:
        ct = subtype.collection_type or "raw_expression"
        methods = subtype.primary_methods or _DEFAULT_METHODS.get(ct, ["serpapi_site"])
        return [StrategyTask(
            taxonomy_lv2=taxonomy_lv2, subtype=subtype.name, collection_type=ct,
            discovery_methods=list(methods),
            preferred_extractors=list(subtype.preferred_extractors),
            preservation_policy=dict(subtype.preservation_policy),
            thresholds=dict(subtype.thresholds),
            target_harm_signals=list(subtype.target_harm_signals),
            seed_urls=list(subtype.seed_urls),
        )]


class Budget:
    """Global/taxonomy/collection_type 별 query·extract quota."""
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.cfg = cfg
        self.used_queries: dict[str, int] = {}
        self.used_extracts: dict[str, int] = {}

    def take(self, kind: str, taxonomy: str, collection_type: str, n: int = 1) -> int:
        used = self.used_queries if kind == "queries" else self.used_extracts
        global_cap = self.cfg.get(f"global_max_{kind}")
        tax_cap = self.cfg.get("per_taxonomy", {}).get(taxonomy, {}).get(f"max_{kind}")
        ct_cap = self.cfg.get("per_collection_type", {}).get(collection_type, {}).get(f"max_{kind}")
        allowed = n
        for key, cap in (("global", global_cap), (f"tax:{taxonomy}", tax_cap),
                         (f"ct:{collection_type}", ct_cap)):
            if cap is not None:
                allowed = min(allowed, max(0, int(cap) - used.get(key, 0)))
        for key in ("global", f"tax:{taxonomy}", f"ct:{collection_type}"):
            used[key] = used.get(key, 0) + allowed
        return allowed
