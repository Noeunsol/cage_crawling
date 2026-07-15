"""공용 데이터 스키마. 모든 extractor 결과는 최종적으로 ContentRecord로 표준화된다 (설계서 §7)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class UrlCandidate:
    """Search API가 만드는 URL 후보 (설계서 §2 Phase 2). Extractor의 입력."""
    source_url: str
    domain: str
    search_query: str
    search_api: str
    taxonomy_lv2_candidate: str
    subtype_candidate: str
    title: Optional[str] = None
    snippet: Optional[str] = None
    published_at_hint: Optional[str] = None
    score: float = 0.0
    # 어떤 수집 경로로 발견됐는지: keyword | semantic | site_sampling | seed_expansion | trend
    collection_method: str = "keyword"
    # frontier 상태: pending/filtered_out/extracting/extracted/failed/matched/stored/review
    status: str = "pending"

    def dedup_key(self) -> str:
        return canonicalize_url(self.source_url)


@dataclass
class ContentRecord:
    """최종 표준 레코드 (설계서 §7). Extractor 출력 → 정제/필터/매칭을 거쳐 저장."""
    source_url: str
    domain: str
    site_name: str
    site_type: str

    taxonomy_lv2_candidate: str
    subtype_candidate: str

    title: str
    body_text: str
    collected_at: str
    search_query: str
    search_api: str
    extractor: str

    published_at: Optional[str] = None
    canonical_url: Optional[str] = None
    collection_method: str = "keyword"

    language: Optional[str] = None
    korea_relevance_score: Optional[float] = None
    taxonomy_relevance_score: Optional[float] = None
    quality_score: Optional[float] = None

    # 매칭 확정 taxonomy (Phase 10 이후 채워짐)
    taxonomy_lv2: Optional[str] = None
    subtype: Optional[str] = None

    dedup_hash: str = ""
    filter_status: str = "pending"   # pass / review / fail / pending
    filter_reason: Optional[str] = None

    raw_html_path: Optional[str] = None
    markdown_path: Optional[str] = None

    content_id: str = ""

    def compute_dedup_hash(self) -> str:
        """canonical url + title + body 앞부분으로 중복 판별 해시 생성."""
        basis = f"{canonicalize_url(self.source_url)}|{self.title}|{self.body_text[:200]}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FilterResult:
    """필터/클리너 반환값. status: pass / fail / review."""
    status: str
    reason: Optional[str] = None
    score: Optional[float] = None


@dataclass
class MatchResult:
    """TaxonomyMatcher 반환값 (설계서 §10)."""
    is_relevant: bool
    taxonomy_lv2: str
    subtype: str
    confidence: float
    reason: str
    safety_flags: list = field(default_factory=list)


def canonicalize_url(url: str) -> str:
    """중복 판별용 정규화: 쿼리스트링/fragment/trailing slash/scheme·www 제거.

    ponytail: 규칙 기반 정규화. tracking 파라미터별 예외가 필요해지면 확장.
    """
    u = url.strip().lower()
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    if u.startswith("www."):
        u = u[4:]
    u = u.split("#", 1)[0].split("?", 1)[0]
    return u.rstrip("/")
