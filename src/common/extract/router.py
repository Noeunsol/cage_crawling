"""추출 사다리. Search가 찾은 URL을 ExtractedContent로 변환.

싼 것부터 시도해 site_type별 성공 기준을 만족하는 첫 rung에서 멈춘다.
fetch는 requests(정적) 1회로 끝나고 모든 rung이 그 HTML을 공유한다 —
"정적으로 안 되면 못 쓴다"가 실질 기준이다(JS 렌더 rung은 v23에서 제거).
"""
from __future__ import annotations

from src.common.fetcher import Fetcher
from src.common.schema import ContentRecord, ExtractedContent, UrlCandidate
from src.common.site_registry import SiteInfo, SiteRegistry
from src.common.extract.base import ExtractionOutcome
from src.common.extract.site_parser import CommunityStaticParser, DcinsidePostExtractor, NaverKinExtractor
from src.common.extract.trafilatura_extractor import TrafilaturaExtractor

# site_type별 rung 이름 순서. 추출은 trafilatura/전용 파서로 통일한다.
_LADDER = {
    "news": ["trafilatura"],
    "blog": ["trafilatura"],
    "tech": ["trafilatura"],
    "qna": ["naver_kin", "trafilatura"],
    "community": ["community"],
    "dynamic": ["community"],
    "unknown": ["trafilatura"],
}

# 전용 parser가 있는 사이트. 없는 사이트는 site_type 기준으로 generic rung을 탄다.
SITE_PARSER_REGISTRY = {
    "naver_kin": "naver_kin",
    "dcinside": "dcinside",
}


class ExtractorRouter:
    def __init__(self, registry: SiteRegistry, settings: dict):
        self.registry = registry
        ex = settings.get("extraction", {})
        self.success_criteria = ex.get("success_criteria", {})
        self.min_default_chars = ex.get("min_extract_chars", 200)
        self.fetcher = Fetcher(settings)
        self._rungs = {
            "naver_kin": NaverKinExtractor(),
            "dcinside": DcinsidePostExtractor(self.fetcher),
            "community": CommunityStaticParser(),
            "trafilatura": TrafilaturaExtractor(),
        }

    def extract(self, c: UrlCandidate, collected_at: str, task=None) -> ExtractionOutcome:
        site = self.registry.lookup(c.domain)
        ladder = self._ladder(site, task)
        html = self.fetcher.fetch(c.source_url)   # 정적 HTML 1회 (정적 rung 공유)
        tried: list[str] = []
        if self.fetcher.last_failure == "robots_disallowed":
            return ExtractionOutcome(record=None, tried=["robots_disallowed"],
                                     reason="robots_disallowed")

        for name in ladder:
            rung = self._rungs[name]
            content = rung.extract(c, site, html)
            if content is None:
                tried.append(f"{name}_fail")
                continue
            ok, why = self._success(content, site.site_type)
            if not ok:
                tried.append(f"{name}_{why}")
                continue
            rec = self._to_record(c, site, content, rung.name, collected_at)
            return ExtractionOutcome(record=rec, tried=tried + [f"{name}_ok"])
        return ExtractionOutcome(record=None, tried=tried, reason="all_extractors_failed")

    def _ladder(self, site: SiteInfo, task=None) -> list[str]:
        site_specific = SITE_PARSER_REGISTRY.get(site.site_name)
        # site_parser: 전용 파서 우선, 없으면 커뮤니티/동적 사이트는 generic community 파서(댓글 보존)
        site_parser = site_specific or ("community" if site.site_type in {"community", "dynamic"} else None)
        aliases = {"site_parser": site_parser, "qna_parser": "naver_kin"}
        requested = list(getattr(task, "preferred_extractors", []) or [])
        requested.insert(0, site.preferred_extractor)
        requested.extend(_LADDER.get(site.site_type, _LADDER["unknown"]))
        ordered = ([site_specific] if site_specific else []) + [aliases.get(x, x) for x in requested]
        return list(dict.fromkeys(x for x in ordered if x in self._rungs))

    def _success(self, content: ExtractedContent, site_type: str) -> tuple[bool, str]:
        crit = self.success_criteria.get(site_type, {})
        min_body = crit.get("min_body_chars", self.min_default_chars)
        body_len = len(content.body_text or "")

        if crit.get("require_title", True) and not (content.title or "").strip():
            return False, "no_title"
        if crit.get("require_date", False) and not content.published_at:
            return False, "no_date"
        if body_len >= min_body:
            return True, "ok"
        return False, f"body_too_short:{body_len}"

    def _to_record(self, c, site, content: ExtractedContent, extractor_name, collected_at) -> ContentRecord:
        rec = ContentRecord(
            source_url=c.source_url,
            domain=c.domain,
            site_name=site.site_name,
            site_type=site.site_type,
            taxonomy_lv2_candidate=c.taxonomy_lv2_candidate,
            subtype_candidate=c.subtype_candidate,
            title=content.title or "",
            body_text=content.body_text,              # clean 단계에서 정제 본문으로 교체
            raw_text=content.body_text,               # 원문 보존
            question_body=getattr(content, "question_body", ""),
            answer_body=getattr(content, "answer_body", ""),
            core_text=getattr(content, "core_text", "") or content.body_text,
            collected_at=collected_at,
            search_query=c.search_query,
            search_api=c.search_api,
            extractor=extractor_name,
            collection_type=c.collection_type,
            discovery_method=c.discovery_method,
            published_at=content.published_at or c.published_at_hint,
            published_at_source=content.published_at_source or ("serpapi_date" if c.published_at_hint else "unknown"),
            value_score=c.value_score,
            extraction_likelihood=c.extraction_likelihood,
            parent_source_url=getattr(c, "parent_source_url", None),
            link_source=getattr(c, "link_source", None),
            is_supplementary=bool(getattr(c, "is_supplementary", False)),
        )
        return rec
