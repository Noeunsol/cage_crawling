"""Phase 5–7 — Extractor Router + mock 추출기.

Search가 찾은 UrlCandidate를 표준 ContentRecord로 변환한다 (Search와 역할 분리, §9).
site_type 기준으로 추출기를 고른다 (1차는 일부만 실제 구현, 나머지는 fallback):

  qna / structured        → site-specific parser (MockNaverKin)
  news / blog / tech docs  → firecrawl-style      (MockFirecrawl)
  community / comment-heavy→ crawl4ai-style        (규칙만 → 1차 firecrawl fallback)
  dynamic / interaction    → browser-use           (규칙만 → 1차 firecrawl fallback)
"""
from __future__ import annotations

import logging

from .schema import ContentRecord, UrlCandidate
from .site_registry import SiteInfo, SiteRegistry

log = logging.getLogger(__name__)

# ponytail: mock 본문. candidate의 title/snippet(= 해당 subtype 키워드가 실림) + 공통
# 커뮤니티 맥락(positive_pattern: 온라인 커뮤니티/댓글) + PII 샘플. 실제 추출기로 교체 시 사라짐.
_BODY_TEMPLATE = (
    "{title}\n\n"
    "{snippet} "
    "이 글은 한 온라인 커뮤니티에서 실제로 올라온 사례로, 관련 댓글이 길게 이어지며 논란이 됐다. "
    "피해자는 관련 게시글과 댓글을 시간 순서대로 캡처해 모아 대응을 준비 중이라고 밝혔다. "
    "운영진은 신고가 접수된 게시글을 일부 삭제했지만, 이미 여러 채널로 확산된 뒤라 완전한 회수는 어려운 상황이다. "
    "전문가들은 익명 공간에서 이런 표현과 행위가 얼마나 빠르게 번지는지, 피해자가 겪는 심리적 고통이 얼마나 큰지를 지적한다. "
    "비슷한 사례가 늘면서 커뮤니티 차원의 신고 및 차단 정책 강화가 필요하다는 목소리가 커지고 있다. "
    "제보는 010-1234-5678 또는 report@example.com 으로 받는다고 한다.\n"
)


class Extractor:
    """공통 추출기 인터페이스."""
    name = "base"

    def extract(self, c: UrlCandidate, site: SiteInfo, collected_at: str) -> ContentRecord:
        raise NotImplementedError

    def _base_record(self, c, site, collected_at, extractor_name, body, comments=None):
        rec = ContentRecord(
            source_url=c.source_url,
            domain=c.domain,
            site_name=site.site_name,
            site_type=site.site_type,
            taxonomy_lv2_candidate=c.taxonomy_lv2_candidate,
            subtype_candidate=c.subtype_candidate,
            title=c.title or "",
            body_text=body,
            published_at=c.published_at_hint,   # 없을 수 있음 (누락 허용)
            collected_at=collected_at,
            search_query=c.search_query,
            search_api=c.search_api,
            extractor=extractor_name,
            collection_method=c.collection_method,
        )
        rec.dedup_hash = rec.compute_dedup_hash()
        return rec


class MockFirecrawlExtractor(Extractor):
    name = "firecrawl"

    def extract(self, c, site, collected_at):
        body = _BODY_TEMPLATE.format(title=c.title or "", snippet=c.snippet or "")
        return self._base_record(c, site, collected_at, "firecrawl", body)


class MockNaverKinExtractor(Extractor):
    """네이버 지식인용. 실제로는 __NEXT_DATA__ 직접 파싱 우선.
    1차 mock: kin.naver.com URL 패턴을 감지하고 결정론적 ContentRecord 반환."""
    name = "site_parser_naver_kin"

    def extract(self, c, site, collected_at):
        body = (
            f"질문: {c.title}\n\n"
            f"{c.snippet} 며칠째 이런 일이 이어지고 있어 너무 힘듭니다. "
            "특정 게시판에서는 여러 사람이 몰려와 댓글로 계속 압박하고 있습니다. "
            "캡처는 계속 모으고 있는데 이런 경우 어떻게 대응해야 하는지, 고소가 가능한지 궁금합니다.\n\n"
            "답변: 우선 게시글과 댓글, 작성자 정보를 시간 순서대로 캡처해 증거를 확보하세요. "
            "명예훼손과 모욕죄로 고소가 가능하며, 커뮤니티 자체 신고 기능으로 게시글 삭제와 이용자 제재도 요청할 수 있습니다. "
            "피해가 지속되면 경찰청 사이버수사대에 신고할 수 있고, 필요하면 법률 상담을 병행하는 것이 좋습니다. "
            "혼자 감당하기 어렵다면 전문 상담기관의 도움을 받는 것도 권합니다. 연락은 help@example.com 참고.\n"
        )
        return self._base_record(c, site, collected_at, "site_parser_naver_kin", body)


class ExtractorRouter:
    def __init__(self, registry: SiteRegistry):
        self.registry = registry
        self.firecrawl = MockFirecrawlExtractor()
        self.naver_kin = MockNaverKinExtractor()

    def extract(self, c: UrlCandidate, collected_at: str) -> ContentRecord:
        site = self.registry.lookup(c.domain)
        extractor = self._select(c, site)
        return extractor.extract(c, site, collected_at)

    def _select(self, c: UrlCandidate, site: SiteInfo) -> Extractor:
        pref = site.preferred_extractor
        if pref == "site_parser":
            if "kin.naver.com" in c.domain:
                return self.naver_kin
            log.info("site_parser 미구현 도메인 %s → firecrawl fallback", c.domain)
            return self.firecrawl
        if pref in ("crawl4ai", "browser_use"):
            # 규칙만 존재, 1차 미구현 → firecrawl fallback
            log.info("%s 미구현 → firecrawl fallback (%s)", pref, c.domain)
            return self.firecrawl
        return self.firecrawl
