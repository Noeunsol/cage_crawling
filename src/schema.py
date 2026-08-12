"""공용 데이터 스키마. 모든 extractor 결과는 최종적으로 ContentRecord로 표준화된다 (설계서 §7)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

from .mask import MaskedEntity


@dataclass
class UrlCandidate:
    """Search API가 만드는 URL 후보 (설계서 §2 Phase 2). Extractor의 입력."""
    source_url: str
    domain: str
    search_query: str
    search_api: str
    taxonomy_lv2_candidate: str
    subtype_candidate: str
    candidate_id: str = ""
    parent_source_url: Optional[str] = None
    link_source: Optional[str] = None          # body | comment
    is_supplementary: bool = False
    title: Optional[str] = None
    snippet: Optional[str] = None
    published_at_hint: Optional[str] = None
    canonical_url: Optional[str] = None
    site_name: str = "unknown"
    site_type: str = "unknown"
    score: float = 0.0                    # frontier 정렬 우선순위(= value_score)
    value_score: float = 0.0              # escalation 게이트용 taxonomy 가치 점수
    extraction_likelihood: float = 1.0    # 정적 추출 성공 가능성 heuristic (0~1)
    # collection_type: raw_expression | qa_consulting | news_case | technical_security
    collection_type: str = "raw_expression"
    discovery_method: str = "serpapi_site"  # board_list|serpapi_site|rss|sitemap|seed_url|tavily|exa|github
    initial_score: float = 0.0
    taxonomy_fit_url_score: float = 0.0
    harm_signal_url_score: float = 0.0
    llm_model: str = ""
    llm_input_tokens: int = 0
    llm_cached_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_total_tokens: int = 0
    llm_estimated_cost_usd: float = 0.0
    source_priority_score: float = 0.0
    reference_page_penalty: float = 0.0
    # ── 2차 semantic discovery (phase-2) ──
    run_id: str = ""
    collection_phase: int = 1                    # 1=trend/keyword, 2=targeted
    query_id: str = ""                           # sha1(discovery_query)[:12]
    discovery_provider: str = ""                 # tavily | exa | serpapi
    discovery_query: Optional[str] = None        # 자연어 collection intent
    discovery_relevance_score: float = 0.0       # rerank 점수 (fetch 전)
    korea_relevance_score: float = 0.0           # rerank 한국 관련성 근사
    content_hint: Optional[str] = None           # provider snippet/content = discovery 메타(본문 아님)
    filter_reason: Optional[str] = None
    # frontier 상태: pending/filtered_out/extracting/extracted/failed/matched/stored/review
    status: str = "pending"
    # 트렌드 모드 부가 메타(source/board_name/bucket/is_trending/view_count/comment_count/like_count).
    # 저장 컬럼이 아니라 extract 후 ContentRecord로 옮기기 위한 운반용.
    meta: dict = field(default_factory=dict)

    def dedup_key(self) -> str:
        return canonicalize_url(self.source_url)


@dataclass
class ExtractedContent:
    """추출기 반환 중간 타입. 저장 스키마(ContentRecord)와 분리해 추출 관심사만 담는다."""
    title: str
    body_text: str
    comments: list = field(default_factory=list)
    published_at: Optional[str] = None
    published_at_source: Optional[str] = None   # serpapi_date|metadata|html_parser|url_pattern|unknown
    author_hint: Optional[str] = None
    view_count: Optional[int] = None
    comment_count: Optional[int] = None
    image_urls: list[str] = field(default_factory=list)


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
    body_text: str          # = masked_text 별칭 (기존 report/CSV/test 호환)
    collected_at: str
    search_query: str
    search_api: str
    extractor: str

    # raw 보존 3단: 욕설·협박은 보존, PII/광고/노이즈만 제거 (Toxic Language raw 가치)
    raw_text: str = ""              # 추출 직후 원문 (미정제, export 기본 제외)
    cleaned_text: str = ""          # boilerplate/메뉴/푸터만 제거
    masked_text: str = ""           # cleaned + PII 마스킹 (matcher/LLM 입력)
    raw_comments: Optional[list] = None
    masked_comments: Optional[list] = None
    original_comment_count: int = 0
    kept_comment_count: int = 0
    duplicate_comments_removed: int = 0
    unrelated_comments_removed: int = 0

    published_at: Optional[str] = None
    published_at_source: Optional[str] = None
    canonical_url: Optional[str] = None
    collection_type: str = "raw_expression"
    discovery_method: str = "serpapi_site"
    value_score: float = 0.0
    extraction_likelihood: float = 0.0

    language: Optional[str] = None
    korea_relevance_score: Optional[float] = None
    taxonomy_relevance_score: Optional[float] = None
    quality_score: Optional[float] = None
    harmfulness_score: Optional[float] = None
    taxonomy_fit_score: Optional[float] = None
    seed_source_value_score: Optional[float] = None
    pii_detected: bool = False
    pii_types: list[str] = field(default_factory=list)
    pii_risk_score: Optional[float] = None
    masking_version: str = ""
    masking_warnings: list[str] = field(default_factory=list)
    masked_entities: list[MaskedEntity] = field(default_factory=list)

    # 매칭 확정 taxonomy (Phase 10 이후 채워짐)
    taxonomy_lv1: Optional[str] = None
    taxonomy_lv2: Optional[str] = None
    subtype: Optional[str] = None

    dedup_hash: str = ""
    simhash: str = ""                # 본문 near-dup (동일 사건) 판별
    event_key: str = ""              # 보조 사건 키 (title+date+site)
    duplicate_of: Optional[str] = None
    filter_status: str = "pending"   # pass / review / fail / pending
    filter_reason: Optional[str] = None
    llm_escalation_reason: Optional[str] = None

    # ── 트렌드 수집 모드 (설계서 v2) ──
    source: str = ""                 # dcinside | fmkorea | natepann | news_rss
    source_type: str = ""            # community | news
    board_name: str = ""             # 갤러리/게시판명
    category_name: str = ""          # 게시판/언론사 카테고리 (taxonomy 아님)
    category: Optional[str] = None    # 공식 taxonomy 하위 라벨 1개 (단일매핑)
    is_risk_candidate: bool = False   # 2차 위험신호 후보 통과 여부
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    dislike_count: Optional[int] = None
    comment_count: Optional[int] = None
    image_urls: list[str] = field(default_factory=list)
    is_trending: bool = False
    risk_score: Optional[int] = None   # 1~5
    trend_score: Optional[int] = None  # 1~5
    confidence: Optional[int] = None   # 1~5 (매처 confidence 0~1 → 1~5)
    action: str = "pending"           # accepted | review | excluded
    is_taxonomy_relevant: bool = False
    is_trend_seed: bool = False
    filter_action: str = "pending"    # 1차 relevance gate: keep | discard
    negative_contexts: list[str] = field(default_factory=list)
    needs_comment_fallback: bool = False
    # 후처리/분류 부가정보 (v8)
    risk_signals: list[str] = field(default_factory=list)      # 감지된 위험신호(toxic/hate/...)
    matched_keywords: list[str] = field(default_factory=list)  # primary category에서 적중한 키워드
    secondary_flags: list[str] = field(default_factory=list)   # 복합 위험(primary 외 신호)
    classification_source: str = "none"   # rule | llm | manual | none
    classification_reason: str = ""       # 왜 이 taxonomy인지
    llm_model: str = ""
    llm_input_tokens: int = 0
    llm_cached_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_total_tokens: int = 0
    llm_estimated_cost_usd: float = 0.0
    is_harmful: Optional[bool] = None
    concrete_context_score: Optional[float] = None
    evidence_spans: list[str] = field(default_factory=list)
    contains_korean_context: Optional[bool] = None
    crawl_status: str = "success"         # success | failed | skipped
    parent_source_url: Optional[str] = None
    link_source: Optional[str] = None
    is_supplementary: bool = False

    # ── 2차 semantic discovery provenance (phase-2). content_hint는 저장하지 않는다(candidate만). ──
    run_id: str = ""
    collection_phase: int = 1                    # 1=trend/keyword, 2=targeted
    query_id: str = ""
    discovery_provider: str = ""                 # tavily | exa | serpapi
    discovery_query: Optional[str] = None        # 자연어 collection intent
    discovery_relevance_score: Optional[float] = None   # rerank 점수(감사용)

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
    taxonomy_lv1: str = ""
    safety_flags: list = field(default_factory=list)
    matched_keywords: list = field(default_factory=list)  # primary category 적중 키워드
    evidence_spans: list = field(default_factory=list)
    source: str = "rule"                                  # rule | llm | manual


def content_id_for(url: str) -> str:
    """source_url → 결정론적 content_id (content_records PK · 단계별 파일 저장 키). 16-hex sha1."""
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]


def canonicalize_url(url: str) -> str:
    """중복 판별용 정규화. 글 식별 query는 보존하고 tracking query만 제거한다."""
    parsed = urlsplit(url.strip())
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    tracking = {"fbclid", "gclid", "ref", "source"}
    if host.endswith("dcinside.com") and parsed.path.rstrip("/").endswith("/board/view"):
        tracking.update({"page", "t", "_dcbest"})
    query = sorted(
        (key.lower(), value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in tracking
    )
    suffix = f"?{urlencode(query)}" if query else ""
    return f"{host}{parsed.path.rstrip('/')}{suffix}"
