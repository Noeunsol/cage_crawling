"""검색으로 찾은 URL 하나를 실제로 가져와 저장할지 결정하는 파이프라인.

fetch/extract부터 저장까지 하나의 흐름으로 묶는다:
URL 중복 체크 → fetch → extract → 본문 중복 체크 → 제외 필터 → 저장.

fetch~extract(네트워크 I/O + 순수 파싱, DB 접근 없음)만 asyncio.to_thread로 동시 실행한다
(2026-08-31). 그 뒤 중복 체크·필터(taxonomy OpenAI 호출 포함)·DB 저장은 지금까지처럼 순차
실행한다 — sqlite3 커넥션 하나를 여러 스레드가 동시에 건드리는 걸 피하고, OpenAI rate limit도
기존과 동일하게 유지하기 위해서다. tavily/serpapi 검색 자체는 여전히 scheduler.py가 순차 호출한다.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

from src.discovery.retry_candidates import find_retryable_candidates
from src.discovery.scheduler import ScheduledCandidate, run_scheduler
from src.extraction import duplicates
from src.extraction.fetcher import FetchError, fetch
from src.extraction.general_extractor import ExtractedContent, ExtractionError
from src.extraction.parser_registry import get_parser, get_source_category
from src.filtering.pipeline import FilterContext, build_filter_chain, run_filters
from src.pipeline.result import ProcessOutcome, ProgressEvent, RunSummary, summarize
from src.query.vocabulary import effective_exclude_criteria
from src.storage.repositories import contents as contents_repo
from src.storage.repositories import discarded as discarded_repo
from src.storage.repositories import discoveries as discoveries_repo
from src.storage.repositories import duplicates as duplicates_repo
from src.storage.repositories import near_duplicate_observations as near_dup_obs_repo
from src.storage.repositories import taxonomy_mappings as mappings_repo
from src.utils import similarity
from src.utils.quota import QuotaExceededError, classify as classify_quota_error
from src.utils.retry_policy_helpers import is_immediately_retryable
from src.utils.text import compute_content_hash
from src.utils.urls import is_blocklisted_domain, is_homepage_url, normalize_url


def _domain(url: str) -> str:
    return urlsplit(url).netloc


def _discard(conn, *, candidate: ScheduledCandidate, run_id: str, normalized_url: str,
             reason: str, retryable: bool, detail: str | None = None) -> ProcessOutcome:
    discarded_repo.record_discarded(
        conn, original_url=candidate.url, normalized_url=normalized_url,
        run_id=run_id, query_id=candidate.query_id, source_domain=_domain(normalized_url),
        reason=reason, retryable=retryable,
    )
    return ProcessOutcome(status="discarded", reason=reason, detail=detail)


def _reuse_for_new_taxonomy(
    conn, candidate: ScheduledCandidate, *, run_id: str, type_cfg: dict,
    date_from: date, date_to: date, filter_checks: list,
) -> ProcessOutcome | None:
    """기존 URL을 다른 taxonomy/type에서 재평가해 같은 콘텐츠 행을 재사용한다."""
    normalized_url = normalize_url(candidate.url)
    content = contents_repo.get_by_canonical_url(conn, normalized_url)
    if content is None:
        return None
    already_mapped = conn.execute(
        """SELECT 1 FROM content_taxonomy_mappings
           WHERE content_id = ? AND taxonomy_lv2 = ? AND type_name = ?""",
        (content["id"], candidate.lv2_id, candidate.type_name),
    ).fetchone()
    if already_mapped:
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=normalized_url,
            reason="duplicate", retryable=False, detail="같은 taxonomy/type에 이미 저장된 URL입니다.",
        )

    ctx = FilterContext(
        canonical_url=normalized_url, source_domain=content["source_domain"], title=content["title"],
        content=content["content"], content_hash=content["content_hash"],
        published_date=content["published_date"], date_from=date_from, date_to=date_to,
        type_name=candidate.type_name, definition=type_cfg["definition"],
        include_criteria=type_cfg["include_criteria"], exclude_criteria=effective_exclude_criteria(type_cfg),
    )
    decision = run_filters(ctx, filter_checks)
    decision_reason = f"{decision.reason}: {decision.detail}" if decision.reason else (decision.detail or "accepted")
    mappings_repo.add_mapping(
        conn, content_id=content["id"], taxonomy_lv2=candidate.lv2_id, type_name=candidate.type_name,
        decision=decision.status, decision_reason=decision_reason,
        prompt_name=None, prompt_version=None, model=None,
    )
    discoveries_repo.record_discovery(
        conn, content_id=content["id"], run_id=run_id, query_id=candidate.query_id,
        provider=candidate.provider, returned_url=candidate.url, rank=candidate.rank,
        relevance_score=candidate.relevance_score,
    )
    return ProcessOutcome(status=decision.status, reason=decision.reason, detail=decision.detail)


@dataclass
class _FetchExtractResult:
    """fetch~extract 단계 결과. DB 접근이 전혀 없어 여러 스레드에서 동시에 만들어도 안전하다.

    성공하면 final_url/domain/source_category/extracted가 채워지고, 실패하면 error_reason이 채워진다.
    """
    final_url: str | None = None
    domain: str | None = None
    source_category: str | None = None
    extracted: ExtractedContent | None = None
    error_reason: str | None = None
    error_retryable: bool = False
    error_detail: str | None = None


def _fetch_and_extract(normalized_url: str, extraction_cfg: dict, retry_policy: dict) -> _FetchExtractResult:
    """conn을 전혀 안 건드리는 순수 fetch+extract. asyncio.to_thread로 동시 호출되는 함수."""
    try:
        fetched = fetch(normalized_url, extraction_cfg, retry_policy)
    except FetchError as e:
        return _FetchExtractResult(error_reason=e.reason, error_retryable=e.retryable)

    final_url = normalize_url(fetched.final_url)
    domain = _domain(final_url)
    source_category = get_source_category(domain)
    min_length = extraction_cfg["min_content_length"].get(
        source_category, extraction_cfg["min_content_length"]["default"],
    )

    try:
        extracted = get_parser(domain)(fetched.html, final_url, min_length, extraction_cfg)
    except ExtractionError as e:
        # dcinside는 성인인증/안내 페이지를 돌려줄 때가 있어 첫 시도만으로 실패 단정하지 않고 한 번 더
        # 받아본다 (2026-09-04, pre 브랜치의 DcinsidePostExtractor 재요청 로직 이식).
        if domain == "gall.dcinside.com":
            try:
                retried = fetch(normalized_url, extraction_cfg, retry_policy)
                extracted = get_parser(domain)(retried.html, final_url, min_length, extraction_cfg)
            except (FetchError, ExtractionError):
                return _FetchExtractResult(
                    final_url=final_url, domain=domain,
                    error_reason=e.reason, error_retryable=is_immediately_retryable(retry_policy["reasons"], e.reason),
                )
        else:
            return _FetchExtractResult(
                final_url=final_url, domain=domain,
                error_reason=e.reason, error_retryable=is_immediately_retryable(retry_policy["reasons"], e.reason),
            )

    return _FetchExtractResult(final_url=final_url, domain=domain, source_category=source_category, extracted=extracted)


async def _fetch_all(
    candidates: list[tuple[int, ScheduledCandidate]],
    *, extraction_cfg: dict, retry_policy: dict, blacklist_domains: list[str],
    max_concurrency: int, max_concurrency_per_domain: int,
) -> list[tuple[int, ScheduledCandidate, _FetchExtractResult]]:
    """중복 체크를 통과한 후보들의 fetch~extract만 동시 실행한다.

    global_sem으로 전체 동시 개수를, domain_sems로 같은 도메인 동시 개수를 제한한다.
    asyncio.gather는 완료 순서와 무관하게 결과를 제출 순서 그대로 돌려주므로, 후보 번호(i) 기준
    재정렬이 따로 필요 없다. 서로 다른 lane(예: 같은 LV2의 type 둘)이 같은 URL을 후보로 내놓는
    경우, url_tasks로 fetch 하나만 실제로 실행하고 나머지는 그 결과를 같이 기다린다(정확히
    같은 URL인 경우에 한함 — 이전 sequential 코드는 앞 후보가 저장한 뒤에야 뒤 후보가 그걸 보고
    건너뛸 수 있었는데, 동시 실행에서는 저장 시점이 늦어져 URL 자체를 공유하는 게 유일한 방법).
    """
    global_sem = asyncio.Semaphore(max_concurrency)
    domain_sems: dict[str, asyncio.Semaphore] = {}
    url_tasks: dict[str, asyncio.Task] = {}

    def _domain_sem(domain: str) -> asyncio.Semaphore:
        if domain not in domain_sems:
            domain_sems[domain] = asyncio.Semaphore(max_concurrency_per_domain)
        return domain_sems[domain]

    async def _fetch_url(normalized_url: str, domain: str) -> _FetchExtractResult:
        if is_blocklisted_domain(domain, blacklist_domains):
            return _FetchExtractResult(
                final_url=normalized_url, domain=domain,
                error_reason="blacklisted_domain", error_retryable=False,
            )
        if is_homepage_url(normalized_url):
            # 검색 API가 특정 기사 대신 사이트 홈페이지를 결과로 돌려줄 때가 있다 — 여러 기사가
            # 뒤섞여 있어 무엇을 추출해도 특정 사례 하나로 신뢰할 수 없으므로 fetch 자체를 안 한다.
            return _FetchExtractResult(
                final_url=normalized_url, domain=domain,
                error_reason="homepage_url", error_retryable=False,
            )
        async with global_sem, _domain_sem(domain):
            try:
                return await asyncio.to_thread(_fetch_and_extract, normalized_url, extraction_cfg, retry_policy)
            except Exception as e:  # noqa: BLE001 - 후보 하나의 예상 못한 예외가 나머지 fetch까지 취소시키면 안 됨
                return _FetchExtractResult(
                    final_url=normalized_url, error_reason="unexpected_error", error_retryable=True,
                    error_detail=f"{type(e).__name__}: {e}",
                )

    async def _one(i: int, candidate: ScheduledCandidate) -> tuple[int, ScheduledCandidate, _FetchExtractResult]:
        normalized_url = normalize_url(candidate.url)
        domain = _domain(normalized_url)
        if normalized_url not in url_tasks:
            url_tasks[normalized_url] = asyncio.create_task(_fetch_url(normalized_url, domain))
        return i, candidate, await url_tasks[normalized_url]

    return await asyncio.gather(*[_one(i, c) for i, c in candidates])


def _finalize_candidate(
    conn: sqlite3.Connection,
    candidate: ScheduledCandidate,
    fr: _FetchExtractResult,
    *,
    run_id: str,
    type_cfg: dict,
    date_from: date,
    date_to: date,
    filter_checks: list,
    fingerprint_cache: list[dict],
    near_duplicate_mode: str = "shadow",
) -> ProcessOutcome:
    """fetch~extract 이후 나머지(본문 중복 체크·필터·저장)를 순차 실행한다. DB/OpenAI를 건드리는
    전부가 여기 있다 — 동시 실행되지 않는다.
    """
    normalized_url = normalize_url(candidate.url)
    if fr.error_reason is not None:
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=fr.final_url or normalized_url,
            reason=fr.error_reason, retryable=fr.error_retryable, detail=fr.error_detail,
        )

    final_url = fr.final_url
    if final_url != normalized_url and duplicates.check_url_duplicate(conn, final_url).is_duplicate:
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=final_url,
            reason="duplicate", retryable=False, detail="리다이렉트된 URL이 이미 저장돼 있습니다.",
        )

    extracted = fr.extracted
    content_hash = compute_content_hash(extracted.title, extracted.content)
    content_dup = duplicates.check_content_duplicate(conn, content_hash)
    if content_dup.is_duplicate:
        duplicates_repo.record_duplicate(
            conn, representative_content_id=content_dup.existing_content_id,
            duplicate_url=final_url, duplicate_reason="same_content_hash",
        )
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=final_url,
            reason="duplicate", retryable=False, detail="다른 URL의 콘텐츠와 본문이 동일합니다.",
        )

    title_normalized = similarity.normalize_title(extracted.title)
    content_fp = similarity.content_fingerprint(extracted.content)
    near_dup = duplicates.check_near_duplicate(fingerprint_cache, title_normalized, content_fp)
    if near_dup.is_duplicate and near_duplicate_mode == "enforce":
        duplicates_repo.record_duplicate(
            conn, representative_content_id=near_dup.matched_content_id,
            duplicate_url=final_url, duplicate_reason="near_duplicate",
            title_similarity=near_dup.title_similarity, content_similarity=near_dup.content_similarity,
        )
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=final_url,
            reason="duplicate", retryable=False,
            detail=(
                f"기존 콘텐츠(id={near_dup.matched_content_id})와 재게시로 추정됩니다 "
                f"(title_sim={near_dup.title_similarity:.2f}, content_sim={near_dup.content_similarity:.2f})."
            ),
        )
    # shadow mode(기본값)에선 위 조건에서 걸러내지 않는다 — 임계값이 아직 100쌍 라벨링으로
    # 검증되기 전이라, 여기서 discard 대신 near_duplicate_observations에 관측만 남긴다
    # (아래 upsert_content 이후, content_id가 생긴 뒤에 기록한다).

    ctx = FilterContext(
        canonical_url=final_url, source_domain=fr.domain, title=extracted.title,
        content=extracted.content, content_hash=content_hash,
        published_date=extracted.published_date, date_from=date_from, date_to=date_to,
        type_name=candidate.type_name, definition=type_cfg["definition"],
        include_criteria=type_cfg["include_criteria"], exclude_criteria=effective_exclude_criteria(type_cfg),
    )
    decision = run_filters(ctx, filter_checks)

    content_id, created = contents_repo.upsert_content(
        conn, title=extracted.title, content=extracted.content,
        published_date=extracted.published_date, canonical_url=final_url,
        source_name=None, source_domain=fr.domain, source_category=fr.source_category,
        status=decision.status, content_hash=content_hash,
        title_normalized=title_normalized, content_fingerprint=similarity.serialize_fingerprint(content_fp),
    )
    if created:
        # 다음 후보의 근사중복 체크가 이 콘텐츠도 보게 하되, DB를 다시 읽지 않고 캐시에만 추가한다.
        fingerprint_cache.append({"id": content_id, "title_normalized": title_normalized, "fingerprint": content_fp})
        if near_dup.best_content_id is not None and near_dup.best_content_similarity >= duplicates.SHADOW_LOG_FLOOR:
            near_dup_obs_repo.record_observation(
                conn, content_id=content_id, matched_content_id=near_dup.best_content_id,
                title_similarity=near_dup.best_title_similarity, content_similarity=near_dup.best_content_similarity,
                would_exclude=near_dup.is_duplicate,
            )
    decision_reason = f"{decision.reason}: {decision.detail}" if decision.reason else (decision.detail or "accepted")
    korea_outcome = decision.outcomes.get("korea_relevance")
    if korea_outcome is not None and korea_outcome.detail:
        decision_reason += f" | 한국 관련성: {korea_outcome.detail}"
    mappings_repo.add_mapping(
        conn, content_id=content_id, taxonomy_lv2=candidate.lv2_id, type_name=candidate.type_name,
        decision=decision.status, decision_reason=decision_reason,
        prompt_name=None, prompt_version=None, model=None,
    )
    discoveries_repo.record_discovery(
        conn, content_id=content_id, run_id=run_id, query_id=candidate.query_id,
        provider=candidate.provider, returned_url=candidate.url, rank=candidate.rank,
        relevance_score=candidate.relevance_score,
    )
    taxonomy_outcome = decision.outcomes.get("taxonomy")
    openai_usage = (
        {
            "prompt_tokens": taxonomy_outcome.prompt_tokens,
            "completion_tokens": taxonomy_outcome.completion_tokens,
            "elapsed_s": taxonomy_outcome.elapsed_s,
        }
        if taxonomy_outcome is not None else None
    )
    return ProcessOutcome(
        status=decision.status, reason=decision.reason, detail=decision.detail, openai_usage=openai_usage,
    )


def process_candidate(
    conn: sqlite3.Connection,
    candidate: ScheduledCandidate,
    *,
    run_id: str,
    type_cfg: dict,
    date_from: date,
    date_to: date,
    extraction_cfg: dict,
    retry_policy: dict,
    filter_checks: list,
    blacklist_domains: list[str] = (),
    fingerprint_cache: list[dict] | None = None,
    near_duplicate_mode: str = "shadow",
) -> ProcessOutcome:
    """후보 하나를 fetch부터 저장까지 순차로 한 번에 처리한다 (단발 호출/테스트용).

    run_collection은 동시성을 위해 fetch~extract(_fetch_all)와 finalize(_finalize_candidate)를
    따로 실행하지만, 후보 하나만 처리하면 되는 경우엔 이 함수가 더 간단하다. 예상 못 한 예외
    (라이브러리 버그 등)가 한 후보 때문에 호출부 전체를 죽이지 않도록, 알려진 예외를 벗어난 건
    여기서 잡아 discarded(reason=unexpected_error)로 기록한다 (2026-08-26: JSONDecodeError
    한 건이 92개 배치 전체를 중단시킨 사고 이후 추가).
    """
    if fingerprint_cache is None:
        fingerprint_cache = duplicates.load_fingerprint_cache(conn)

    normalized_url = normalize_url(candidate.url)
    if is_blocklisted_domain(_domain(normalized_url), blacklist_domains):
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=normalized_url,
            reason="blacklisted_domain", retryable=False, detail="블랙리스트 도메인이라 요청을 보내지 않았습니다.",
        )
    if is_homepage_url(normalized_url):
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=normalized_url,
            reason="homepage_url", retryable=False, detail="사이트 홈페이지라 특정 기사로 신뢰할 수 없어 요청을 보내지 않았습니다.",
        )
    reused = _reuse_for_new_taxonomy(
        conn, candidate, run_id=run_id, type_cfg=type_cfg, date_from=date_from,
        date_to=date_to, filter_checks=filter_checks,
    )
    if reused is not None:
        return reused

    try:
        fr = _fetch_and_extract(normalized_url, extraction_cfg, retry_policy)
    except Exception as e:  # noqa: BLE001
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=normalized_url,
            reason="unexpected_error", retryable=True, detail=f"{type(e).__name__}: {e}",
        )

    try:
        return _finalize_candidate(
            conn, candidate, fr, run_id=run_id, type_cfg=type_cfg, date_from=date_from, date_to=date_to,
            filter_checks=filter_checks, fingerprint_cache=fingerprint_cache,
            near_duplicate_mode=near_duplicate_mode,
        )
    except QuotaExceededError:
        raise  # OpenAI 사용량 초과는 이 후보만의 문제가 아니라 호출부가 처리해야 한다.
    except Exception as e:
        quota_error = classify_quota_error("openai", e)
        if quota_error is not None:
            raise quota_error from e
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=normalized_url,
            reason="unexpected_error", retryable=True, detail=f"{type(e).__name__}: {e}",
        )


def run_collection(
    conn: sqlite3.Connection,
    providers: dict,
    configs: dict,
    run_id: str,
    targets: list[tuple[str, str]],
    *,
    target_count: int,
    candidate_multiplier: float,
    date_range_by_lv2: dict[str, tuple[date, date]],
    provider_ratio_by_lv2: dict[str, dict],
    openai_client,
    on_progress: Callable[[ProgressEvent], None] | None = None,
    max_calls_by_provider: dict[str, int] | None = None,
    use_adaptive_multiplier: bool = False,
    adaptive_multiplier_snapshot: dict[str, float] | None = None,
) -> RunSummary:
    """검색(scheduler) → 후보별 처리(fetch~extract는 동시, 나머지는 순차) 순서로 실행한다.

    검색 단계가 먼저 전부 끝난 뒤 추출·필터링 단계가 진행된다.
    on_progress를 넘기면 후보를 하나 마무리할 때마다 즉시 호출된다 (Streamlit 진행률 표시용).
    max_calls_by_provider는 이번 실행만의 provider별 실제 API 호출 상한이다 (실험용).
    """
    model = configs["providers"]["openai"]["model"]
    near_duplicate_mode = configs.get("collection", {}).get("near_duplicate_detection", {}).get("mode", "shadow")
    scheduler_result = run_scheduler(
        conn, providers, configs, run_id, targets,
        target_count=target_count, candidate_multiplier=candidate_multiplier,
        date_range_by_lv2=date_range_by_lv2, provider_ratio_by_lv2=provider_ratio_by_lv2,
        max_calls_by_provider=max_calls_by_provider, use_adaptive_multiplier=use_adaptive_multiplier,
        adaptive_multiplier_snapshot=adaptive_multiplier_snapshot,
    )

    filter_checks = build_filter_chain(
        blacklist_domains=configs["blacklist"]["domains"],
        openai_client=openai_client, model=model,
        min_korean_ratio=configs["extraction"]["korea_relevance"]["min_korean_ratio"],
        enable_taxonomy_filter=configs["extraction"]["taxonomy_filter"]["enabled"],
    )
    taxonomy_lookup = {
        (g["lv2_id"], t["name"]): t
        for g in configs["taxonomy"]["taxonomy"] for t in g["types"]
    }
    # 근사중복 체크용 캐시 — 한 번만 읽고 이 run 안에서 새로 저장되는 콘텐츠는 append로 갱신한다
    # (2026-08-31 성능 개선: 예전엔 candidate마다 전체 지문 테이블을 다시 읽고 다시 파싱했다).
    fingerprint_cache = duplicates.load_fingerprint_cache(conn)

    # 이전 run에서 retryable=1로 discard된 후보를 새 검색 없이 다시 큐에 올린다. max_attempts를 넘겼거나 이미 저장된 건 find_retryable_candidates가 알아서 뺀다.
    retry_candidates = find_retryable_candidates(conn, configs, targets)
    if retry_candidates:
        scheduler_result.warnings.append(f"이전 실패 중 재시도 대상 {len(retry_candidates)}건을 다시 큐에 올렸습니다.")

    candidates = scheduler_result.candidates + retry_candidates
    total = len(candidates)
    fetch_cfg = configs.get("retry_policy", {}).get("rate_limit", {}).get("fetch", {})
    max_concurrency = fetch_cfg.get("max_concurrency", 5)
    max_concurrency_per_domain = fetch_cfg.get("max_concurrency_per_domain", 2)
    # fetch를 CHUNK_SIZE만큼씩 나눠 돌리고 그때마다 바로 finalize한다 — 통째로 다 fetch한
    # 다음에 finalize하면, 시스템 장애로 fetch가 계속 실패하는 상황에서 브레이커가 배치 전체를
    # 다 쓴 뒤에야 작동한다(2026-08-31 리뷰에서 발견 — 동시성 도입으로 생긴 회귀. 원래 브레이커는
    # 2026-08-26 JSONDecodeError 사고 이후 만든 안전장치인데, 청크로 낭비 범위를 제한해서 살린다).
    CHUNK_SIZE = 25

    events: list[ProgressEvent] = []
    warnings = list(scheduler_result.warnings)
    consecutive_unexpected_errors = 0
    UNEXPECTED_ERROR_BREAKER = 5
    stopped = False

    # scheduler는 검색 결과를 페이지 단위(최대 20/10건)로만 받아올 수 있어 target_count보다
    # 훨씬 많은 원시 후보가 넘어올 수 있다 — save_over_target_results=false면 (lv2,target_count가
    # 이미 채워진) 남는 후보는 fetch/추출/OpenAI 없이 바로 건너뛴다(configs/collection.yaml
    # scheduling.save_over_target_results, 2026-09-01 이전엔 설정만 있고 코드가 안 읽던 죽은 값).
    save_over_target = configs.get("collection", {}).get("scheduling", {}).get("save_over_target_results", True)
    accepted_by_lv2: Counter = Counter()

    for chunk_start in range(0, total, CHUNK_SIZE):
        if stopped:
            break
        chunk = list(enumerate(candidates))[chunk_start:chunk_start + CHUNK_SIZE]

        # 이미 저장된 URL로 확인되는 후보는 fetch 없이 바로 discarded 처리한다 (동시 fetch를
        # 시작하기 전, 순차적으로 conn을 읽는 유일한 지점 — 예전부터 있던 최적화를 그대로 유지).
        pre_resolved: dict[int, ProcessOutcome] = {}
        to_fetch: list[tuple[int, ScheduledCandidate]] = []
        for i, candidate in chunk:
            if not save_over_target and accepted_by_lv2[candidate.lv2_id] >= target_count:
                pre_resolved[i] = _discard(
                    conn, candidate=candidate, run_id=run_id, normalized_url=normalize_url(candidate.url),
                    reason="target_reached", retryable=False, detail="lv2 목표 수집량에 이미 도달했습니다.",
                )
                continue
            normalized_url = normalize_url(candidate.url)
            type_cfg = taxonomy_lookup[(candidate.lv2_id, candidate.type_name)]
            date_from, date_to = date_range_by_lv2[candidate.lv2_id]
            reused = _reuse_for_new_taxonomy(
                conn, candidate, run_id=run_id, type_cfg=type_cfg,
                date_from=date_from, date_to=date_to, filter_checks=filter_checks,
            )
            if reused is None:
                to_fetch.append((i, candidate))
            else:
                pre_resolved[i] = reused

        fetch_results = asyncio.run(_fetch_all(
            to_fetch,
            extraction_cfg=configs["extraction"], retry_policy=configs["retry_policy"],
            blacklist_domains=configs["blacklist"]["domains"],
            max_concurrency=max_concurrency, max_concurrency_per_domain=max_concurrency_per_domain,
        )) if to_fetch else []
        fetch_results_by_index = {i: fr for i, _, fr in fetch_results}

        for i, candidate in chunk:
            if i in pre_resolved:
                outcome = pre_resolved[i]
            else:
                fr = fetch_results_by_index[i]
                type_cfg = taxonomy_lookup[(candidate.lv2_id, candidate.type_name)]
                date_from, date_to = date_range_by_lv2[candidate.lv2_id]
                try:
                    outcome = _finalize_candidate(
                        conn, candidate, fr, run_id=run_id, type_cfg=type_cfg,
                        date_from=date_from, date_to=date_to, filter_checks=filter_checks,
                        fingerprint_cache=fingerprint_cache, near_duplicate_mode=near_duplicate_mode,
                    )
                except QuotaExceededError as quota_error:
                    warnings.append(
                        f"{quota_error.provider}: API 사용량 한도를 초과해 실행을 중단했습니다. "
                        f"지금까지 처리한 {len(events)}/{total}건은 그대로 저장되어 있습니다."
                    )
                    stopped = True
                    break
                except Exception as e:
                    quota_error = classify_quota_error("openai", e)
                    if quota_error is not None:
                        warnings.append(
                            f"{quota_error.provider}: API 사용량 한도를 초과해 실행을 중단했습니다. "
                            f"지금까지 처리한 {len(events)}/{total}건은 그대로 저장되어 있습니다."
                        )
                        stopped = True
                        break
                    outcome = _discard(
                        conn, candidate=candidate, run_id=run_id, normalized_url=normalize_url(candidate.url),
                        reason="unexpected_error", retryable=True, detail=f"{type(e).__name__}: {e}",
                    )

            if outcome.status == "accepted":
                accepted_by_lv2[candidate.lv2_id] += 1

            event = ProgressEvent(
                lv2_id=candidate.lv2_id, type_name=candidate.type_name, url=candidate.url,
                outcome=outcome, processed=i + 1, total=total,
            )
            events.append(event)
            if on_progress is not None:
                on_progress(event)

            if outcome.reason == "unexpected_error":
                consecutive_unexpected_errors += 1
                if consecutive_unexpected_errors >= UNEXPECTED_ERROR_BREAKER:
                    warnings.append(
                        f"알 수 없는 오류가 {UNEXPECTED_ERROR_BREAKER}건 연속 발생해 실행을 중단했습니다"
                        f"(마지막 오류: {outcome.detail}). 개별 후보 문제가 아니라 설정/코드 문제일 수 있습니다. "
                        f"지금까지 처리한 {len(events)}/{total}건은 그대로 저장되어 있습니다."
                    )
                    stopped = True
                    break
            else:
                consecutive_unexpected_errors = 0

    return summarize(
        run_id, events, provider_usage=scheduler_result.provider_usage, warnings=warnings,
    )
