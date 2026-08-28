"""검색으로 찾은 URL 하나를 실제로 가져와 저장할지 결정하는 파이프라인.

Phase 7(fetch/extract)~9(export 대상 데이터)를 하나의 흐름으로 묶는다:
URL 중복 체크 → fetch → extract → 본문 중복 체크 → 제외 필터 → 저장.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from urllib.parse import urlsplit

from src.discovery.scheduler import ScheduledCandidate, run_scheduler
from src.extraction import duplicates
from src.extraction.fetcher import FetchError, fetch
from src.extraction.general_extractor import ExtractionError
from src.extraction.parser_registry import get_parser, get_source_category
from src.filtering.pipeline import FilterContext, build_filter_chain, run_filters
from src.pipeline.result import ProcessOutcome, ProgressEvent, RunSummary, summarize
from src.query.vocabulary import effective_exclude_criteria
from src.storage.repositories import contents as contents_repo
from src.storage.repositories import discarded as discarded_repo
from src.storage.repositories import discoveries as discoveries_repo
from src.storage.repositories import duplicates as duplicates_repo
from src.storage.repositories import taxonomy_mappings as mappings_repo
from src.utils.quota import QuotaExceededError, classify as classify_quota_error
from src.utils.text import compute_content_hash
from src.utils.urls import is_blocklisted_domain, normalize_url


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
) -> ProcessOutcome:
    """후보 하나를 처리한다. 예상 못 한 예외(라이브러리 버그 등)가 한 후보 때문에 전체 실행을
    통째로 죽이지 않도록, 알려진 예외(FetchError/ExtractionError)를 벗어난 것은 여기서 잡아
    discarded(reason=unexpected_error)로 기록하고 다음 후보로 넘어간다 (2026-08-26: JSONDecodeError
    한 건이 92개 배치 전체를 중단시킨 사고 이후 추가).
    """
    try:
        return _process_candidate(
            conn, candidate, run_id=run_id, type_cfg=type_cfg, date_from=date_from, date_to=date_to,
            extraction_cfg=extraction_cfg, retry_policy=retry_policy, filter_checks=filter_checks,
            blacklist_domains=blacklist_domains,
        )
    except QuotaExceededError:
        raise  # OpenAI 사용량 초과는 이 후보만의 문제가 아니라 실행 전체를 멈춰야 한다 — run_collection에서 처리.
    except Exception as e:
        quota_error = classify_quota_error("openai", e)
        if quota_error is not None:
            raise quota_error from e
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=normalize_url(candidate.url),
            reason="unexpected_error", retryable=True, detail=f"{type(e).__name__}: {e}",
        )


def _process_candidate(
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
) -> ProcessOutcome:
    normalized_url = normalize_url(candidate.url)

    url_dup = duplicates.check_url_duplicate(conn, normalized_url)
    if url_dup.is_duplicate:
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=normalized_url,
            reason="duplicate", retryable=False, detail="이미 저장된 URL입니다.",
        )

    # 블랙리스트 도메인은 fetch/추출을 시도조차 하지 않는다 — 유튜브·인스타그램처럼 애초에
    # 정적으로 못 가져오는 사이트가 fetch까지 갔다가 extraction_failed로 낭비되는 걸 막는다
    # (2026-08-26 실측: youtube.com 9건, instagram.com 4건이 이 경로로 새고 있었음).
    if is_blocklisted_domain(_domain(normalized_url), blacklist_domains):
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=normalized_url,
            reason="blacklisted_domain", retryable=False, detail="블랙리스트 도메인이라 요청을 보내지 않았습니다.",
        )

    try:
        fetched = fetch(normalized_url, extraction_cfg, retry_policy)
    except FetchError as e:
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=normalized_url,
            reason=e.reason, retryable=e.retryable,
        )

    final_url = normalize_url(fetched.final_url)
    if final_url != normalized_url and duplicates.check_url_duplicate(conn, final_url).is_duplicate:
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=final_url,
            reason="duplicate", retryable=False, detail="리다이렉트된 URL이 이미 저장돼 있습니다.",
        )

    domain = _domain(final_url)
    source_category = get_source_category(domain)
    min_length = extraction_cfg["min_content_length"].get(
        source_category, extraction_cfg["min_content_length"]["default"],
    )

    try:
        extracted = get_parser(domain)(fetched.html, final_url, min_length, extraction_cfg)
    except ExtractionError as e:
        return _discard(
            conn, candidate=candidate, run_id=run_id, normalized_url=final_url,
            reason=e.reason, retryable=retry_policy["reasons"][e.reason]["retryable"],
        )

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

    ctx = FilterContext(
        canonical_url=final_url, source_domain=domain, title=extracted.title,
        content=extracted.content, content_hash=content_hash,
        published_date=extracted.published_date, date_from=date_from, date_to=date_to,
        type_name=candidate.type_name, definition=type_cfg["definition"],
        include_criteria=type_cfg["include_criteria"], exclude_criteria=effective_exclude_criteria(type_cfg),
    )
    decision = run_filters(ctx, filter_checks)

    content_id, _ = contents_repo.upsert_content(
        conn, title=extracted.title, content=extracted.content,
        published_date=extracted.published_date, canonical_url=final_url,
        source_name=None, source_domain=domain, source_category=source_category,
        status=decision.status, content_hash=content_hash,
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
) -> RunSummary:
    """검색(scheduler) → 후보별 처리(process_candidate) 순서로 실행한다.

    검색 단계가 먼저 전부 끝난 뒤 추출·필터링 단계가 후보별로 진행된다 (두 단계로 분리 — 7.4절).
    on_progress를 넘기면 후보를 하나 처리할 때마다 즉시 호출된다 (Streamlit 진행률 표시용).
    max_calls_by_provider는 이번 실행만의 provider별 실제 API 호출 상한이다 (실험용).
    """
    model = configs["providers"]["openai"]["model"]
    scheduler_result = run_scheduler(
        conn, providers, configs, run_id, targets,
        target_count=target_count, candidate_multiplier=candidate_multiplier,
        date_range_by_lv2=date_range_by_lv2, provider_ratio_by_lv2=provider_ratio_by_lv2,
        max_calls_by_provider=max_calls_by_provider,
    )

    filter_checks = build_filter_chain(
        conn=conn, blacklist_domains=configs["blacklist"]["domains"],
        openai_client=openai_client, model=model,
        min_korean_ratio=configs["extraction"]["korea_relevance"]["min_korean_ratio"],
        enable_taxonomy_filter=configs["extraction"]["taxonomy_filter"]["enabled"],
    )
    taxonomy_lookup = {
        (g["lv2_id"], t["name"]): t
        for g in configs["taxonomy"]["taxonomy"] for t in g["types"]
    }

    events: list[ProgressEvent] = []
    warnings = list(scheduler_result.warnings)
    total = len(scheduler_result.candidates)
    consecutive_unexpected_errors = 0
    # process_candidate는 라이브러리 버그 등 "알려지지 않은" 예외를 후보 1건의 discarded로
    # 바꿔서 삼킨다(unexpected_error) — 그래야 후보 하나의 버그가 전체 실행을 멈추지 않는다.
    # 하지만 그게 연속으로 계속 나오면 후보가 아니라 코드/설정 자체가 깨졌다는 신호라, 남은
    # 후보를 전부 같은 이유로 낭비하기 전에 멈추고 사용자에게 알린다.
    UNEXPECTED_ERROR_BREAKER = 5
    for i, candidate in enumerate(scheduler_result.candidates, start=1):
        type_cfg = taxonomy_lookup[(candidate.lv2_id, candidate.type_name)]
        date_from, date_to = date_range_by_lv2[candidate.lv2_id]
        try:
            outcome = process_candidate(
                conn, candidate, run_id=run_id, type_cfg=type_cfg,
                date_from=date_from, date_to=date_to,
                extraction_cfg=configs["extraction"], retry_policy=configs["retry_policy"],
                filter_checks=filter_checks, blacklist_domains=configs["blacklist"]["domains"],
            )
        except QuotaExceededError as quota_error:
            # openai 사용량이 소진되면 남은 후보를 계속 돌려봤자 전부 같은 이유로 실패한다 —
            # 여기서 멈추고 지금까지 처리한 결과(이미 DB에 반영됨)를 그대로 돌려준다.
            warnings.append(
                f"{quota_error.provider}: API 사용량 한도를 초과해 실행을 중단했습니다. "
                f"지금까지 처리한 {len(events)}/{total}건은 그대로 저장되어 있습니다."
            )
            break
        event = ProgressEvent(
            lv2_id=candidate.lv2_id, type_name=candidate.type_name, url=candidate.url,
            outcome=outcome, processed=i, total=total,
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
                break
        else:
            consecutive_unexpected_errors = 0

    return summarize(
        run_id, events, provider_usage=scheduler_result.provider_usage, warnings=warnings,
    )
