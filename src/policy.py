"""Phase 0 — Taxonomy Policy 로더. enabled:true 정책만 반환."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml


@dataclass
class Subtype:
    name: str
    description: str = ""
    keywords: list[str] = None
    positive_patterns: list[str] = None
    negative_patterns: list[str] = None
    priority_sites: list[str] = None
    semantic_queries: list[str] = None      # 의미 기반 검색용 자연어 쿼리
    seed_boards: list[dict] = None          # 사이트/게시판 샘플링용 [{site, board, mode}]
    preferred_search_api: dict = None       # {primary, fallback:[...]}
    preferred_extractors: dict = None       # {primary, fallback:[...]}
    safety_rule: dict = None                # {pii_masking, image_collection}

    def __post_init__(self):
        # None 리스트/딕트를 빈 값으로 정규화
        self.keywords = self.keywords or []
        self.positive_patterns = self.positive_patterns or []
        self.negative_patterns = self.negative_patterns or []
        self.priority_sites = self.priority_sites or []
        self.semantic_queries = self.semantic_queries or []
        self.seed_boards = self.seed_boards or []
        self.preferred_search_api = self.preferred_search_api or {"primary": "serpapi", "fallback": []}
        self.preferred_extractors = self.preferred_extractors or {"primary": "firecrawl", "fallback": []}
        self.safety_rule = self.safety_rule or {"pii_masking": True, "image_collection": False}

    @property
    def search_apis(self) -> list[str]:
        """primary + fallback 순서의 검색 API 리스트."""
        api = self.preferred_search_api
        return [api["primary"], *api.get("fallback", [])]


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
        subtypes = [Subtype(**_subtype_kwargs(s)) for s in p.get("subtypes", [])]
        policies.append(Policy(taxonomy_lv2=p["taxonomy_lv2"], subtypes=subtypes))
    return policies


def _subtype_kwargs(s: dict) -> dict:
    """YAML dict에서 Subtype 필드만 추린다 (알 수 없는 키 무시)."""
    allowed = {
        "name", "description", "keywords", "positive_patterns", "negative_patterns",
        "priority_sites", "semantic_queries", "seed_boards",
        "preferred_search_api", "preferred_extractors", "safety_rule",
    }
    return {k: v for k, v in s.items() if k in allowed}
