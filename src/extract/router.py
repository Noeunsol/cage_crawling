"""추출 사다리 (Cost-Escalation Ladder). Search가 찾은 URL을 ExtractedContent로 변환.

싼 것부터 시도해 site_type별 성공 기준을 만족하는 첫 rung에서 멈춘다.
fetch는 requests(정적)/Playwright(JS 렌더), 추출은 trafilatura로 통일한다.
Playwright/Firecrawl은 config로 gated(기본 off), lib은 lazy import.
Firecrawl은 value_score >= min_value(가치 게이트)일 때만 시도.
"""
from __future__ import annotations

from ..fetcher import Fetcher
from ..image_ocr import ImageOCR
from ..schema import ContentRecord, ExtractedContent, UrlCandidate
from ..site_registry import SiteInfo, SiteRegistry
from .base import ExtractionOutcome
from .playwright_extractor import FirecrawlExtractor, PlaywrightExtractor
from .site_parser import CommunityStaticParser, DcinsidePostExtractor, NaverKinExtractor
from .trafilatura_extractor import TrafilaturaExtractor

# site_type별 rung 이름 순서 (fetch=정적/렌더, 추출=trafilatura로 통일).
_LADDER = {
    "news": ["trafilatura", "playwright", "firecrawl"],
    "blog": ["trafilatura", "playwright", "firecrawl"],
    "tech": ["trafilatura", "playwright", "firecrawl"],
    "qna": ["naver_kin", "trafilatura", "playwright", "firecrawl"],
    "community": ["community", "playwright", "firecrawl"],
    "dynamic": ["community", "playwright", "firecrawl"],
    "unknown": ["trafilatura", "playwright", "firecrawl"],
}
_GATED_RUNGS = {"playwright", "firecrawl"}

# collection_type별 추출기 우선 사다리
_COLLECTION_TYPE_LADDERS = {
    "raw_expression": ["community", "playwright", "trafilatura"],
    "qa_consulting": ["naver_kin", "trafilatura", "playwright"],
    "news_case": ["trafilatura", "community", "firecrawl"],
    "technical_security": ["trafilatura", "community", "playwright"],
}

# 실제 전용 parser가 추가될 때 값만 전용 rung으로 교체한다.
SITE_PARSER_REGISTRY = {
    "naver_kin": "naver_kin",
    "nate_pann": None,
    "dcinside": "dcinside",
    "fmkorea": None,
    "inven": None,
    "lawtalk": None,
}


class ExtractorRouter:
    def __init__(self, registry: SiteRegistry, settings: dict):
        self.registry = registry
        ex = settings.get("extraction", {})
        self.success_criteria = ex.get("success_criteria", {})
        self.min_default_chars = ex.get("min_extract_chars", 200)
        cc = ex.get("comments", {})
        self.max_comments = cc.get("max_comments", 200)
        self.max_comment_chars = cc.get("max_comment_chars", 500)
        self.fetcher = Fetcher(settings)
        self.image_ocr = ImageOCR(self.fetcher, ex.get("image_ocr", {}))
        self._rungs = {
            "naver_kin": NaverKinExtractor(),
            "dcinside": DcinsidePostExtractor(self.fetcher, self.max_comments),
            "community": CommunityStaticParser(),
            "trafilatura": TrafilaturaExtractor(),
            "playwright": PlaywrightExtractor(ex.get("playwright", {})),
            "firecrawl": FirecrawlExtractor(ex.get("firecrawl", {})),
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
            if name in _GATED_RUNGS:
                if not rung.enabled:
                    tried.append(f"{name}_skip")
                    continue
                if name == "firecrawl" and c.value_score < rung.min_value:
                    tried.append(f"{name}_skip_lowvalue")
                    continue
            content = rung.extract(c, site, html)
            if content is None:
                tried.append(f"{name}_fail")
                continue
            if site.site_name == "dcinside":
                self.image_ocr.enrich(content, getattr(c, "filter_action", "pending"))
            if site.site_name == "dcinside" and name != "dcinside":
                # 전용 파서가 일시적 응답으로 실패해도 범용 본문 결과에 댓글을 재결합한다.
                fresh_html = self.fetcher.fetch(c.source_url)
                enriched = self._rungs["dcinside"].extract(c, site, fresh_html)
                if enriched and enriched.comments:
                    content.comments = enriched.comments
                    content.comment_count = len(enriched.comments)
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
        aliases = {"site_parser": site_parser, "qna_parser": "naver_kin",
                   "firecrawl_optional": "firecrawl", "playwright_optional": "playwright"}
        requested = list(getattr(task, "preferred_extractors", []) or [])
        requested.insert(0, site.preferred_extractor)
        requested.extend(_COLLECTION_TYPE_LADDERS.get(getattr(task, "collection_type", ""), []))
        requested.extend(_LADDER.get(site.site_type, _LADDER["unknown"]))
        ordered = ([site_specific] if site_specific else []) + [aliases.get(x, x) for x in requested]
        return list(dict.fromkeys(x for x in ordered if x in self._rungs))

    def _success(self, content: ExtractedContent, site_type: str) -> tuple[bool, str]:
        crit = self.success_criteria.get(site_type, {})
        min_body = crit.get("min_body_chars", self.min_default_chars)
        body_len = len(content.body_text or "")
        comments_len = sum(len(x) for x in (content.comments or []))

        if crit.get("require_title", True) and not (content.title or "").strip():
            return False, "no_title"
        if crit.get("require_date", False) and not content.published_at:
            return False, "no_date"
        if body_len >= min_body:
            return True, "ok"
        # 커뮤니티: 본문 짧아도 댓글이 충분하면 성공
        if crit.get("allow_comment_only") and comments_len >= crit.get("min_comment_chars", 100):
            return True, "ok"
        return False, f"body_too_short:{body_len}"

    def _to_record(self, c, site, content: ExtractedContent, extractor_name, collected_at) -> ContentRecord:
        # 댓글 상한 적용 (count는 원래 값 보존)
        comments = list((content.comments or [])[: self.max_comments])
        if self.max_comment_chars > 0:  # 0이면 댓글 전문 보존
            comments = [x[: self.max_comment_chars] for x in comments]
        rec = ContentRecord(
            source_url=c.source_url,
            domain=c.domain,
            site_name=site.site_name,
            site_type=site.site_type,
            taxonomy_lv2_candidate=c.taxonomy_lv2_candidate,
            subtype_candidate=c.subtype_candidate,
            title=content.title or "",
            body_text=content.body_text,              # clean 단계에서 masked_text로 교체
            raw_text=content.body_text,               # 원문 보존
            raw_comments=comments or None,
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
            image_urls=list(getattr(content, "image_urls", []) or []),
            ocr_image_count=int(getattr(content, "ocr_image_count", 0) or 0),
            ocr_char_count=int(getattr(content, "ocr_char_count", 0) or 0),
            parent_source_url=getattr(c, "parent_source_url", None),
            link_source=getattr(c, "link_source", None),
            is_supplementary=bool(getattr(c, "is_supplementary", False)),
        )
        return rec
