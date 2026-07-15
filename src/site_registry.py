"""site_policy.yaml 로드 → 도메인 판별 및 ExtractorRouter 분기 정보 제공."""
from __future__ import annotations

from dataclasses import dataclass

import yaml


@dataclass
class SiteInfo:
    site_name: str
    site_type: str
    preferred_extractor: str


_UNKNOWN = SiteInfo(site_name="unknown", site_type="unknown", preferred_extractor="firecrawl")


class SiteRegistry:
    def __init__(self, sites: dict[str, SiteInfo], primary_domain: dict[str, str] | None = None):
        # domain(suffix) → SiteInfo
        self._by_domain = sites
        # site_name → 대표 도메인 (query 생성용)
        self._primary_domain = primary_domain or {}

    @classmethod
    def load(cls, path: str) -> "SiteRegistry":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        by_domain: dict[str, SiteInfo] = {}
        primary_domain: dict[str, str] = {}
        for site_name, cfg in data.get("sites", {}).items():
            info = SiteInfo(
                site_name=site_name,
                site_type=cfg.get("site_type", "unknown"),
                preferred_extractor=cfg.get("preferred_extractor", "firecrawl"),
            )
            domains = cfg.get("domains", [])
            for domain in domains:
                by_domain[domain.lower()] = info
            if domains:
                primary_domain[site_name] = domains[0].lower()
        return cls(by_domain, primary_domain)

    def lookup(self, domain: str) -> SiteInfo:
        """도메인 suffix 매칭. gall.dcinside.com → dcinside.com 규칙 포함."""
        d = domain.lower()
        if d.startswith("www."):
            d = d[4:]
        for known, info in self._by_domain.items():
            if d == known or d.endswith("." + known):
                return info
        return _UNKNOWN

    def domain_for(self, site_name: str) -> str | None:
        """site_name → 대표 도메인 (query 생성용). 미등록이면 None."""
        return self._primary_domain.get(site_name)
