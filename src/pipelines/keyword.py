"""keyword(레거시) 모드 실행 — taxonomy 검색 → 추출 → 확인 분류 → 저장."""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import uuid
from collections import defaultdict

import yaml

from ..reporting import coverage as _coverage
from ..clean import clean_record
from ..storage.dedup import EventDeduper, make_event_key, simhash
from ..discovery import DiscoveryRouter
from ..discovery.board import discover_dcinside_trend
from ..discovery.rss import discover_news_trend
from ..extract import ExtractorRouter
from ..keyword_discovery.frontier import UrlFrontier
from ..classify.matcher import build_taxonomy_index, confidence_bucket, risk_score_of, trend_score_of
from ..artifact_store import ArtifactStore
from ..mask import BasicPIIMasker
from ..phase2.intent_builder import build_collection_intent
from ..phase2.provider import MockTavilyProvider, TavilyProvider, to_candidate
from ..phase2.reranker import rerank
from ..policy import load_policies
from ..keyword_discovery.query import QueryGenerator
from ..filtering.quality import QualityFilter
from ..filtering.relevance_filter import RELEVANCE_SIGNAL_TO_LV2, decide_candidate_action, decide_filter_action
from ..reporting.report import build_report, export_csv, export_report, print_report
from ..schema import canonicalize_url
from ..site_registry import SiteRegistry
from ..storage.store import Store
from ..keyword_discovery.strategy import Budget, StrategyRouter
from ..filtering.url_filter import UrlFilter
from .persist import _finalize, _phase2_store
from .stages import _apply_trend_meta, _build_matcher, _copy_llm_usage, _TREND_PRESERVATION
from .taxonomy_adjudication import (
    _classification_status, _phase2_adjudicate, _trend_classification_action,
)
from ._trend_util import (
    _allocate_buckets, _append_unique_candidates,
    _candidate_in_window, _days_old, _extract_links, _link_candidate,
    _published_in_window, _round_robin_candidates, _target_stats,
)

log = logging.getLogger(__name__)

def run(
    taxonomy_config: str = "configs/taxonomy.yaml",
    site_config: str = "configs/site_policy.yaml",
    settings_config: str = "configs/crawler_settings.yaml",
    db_path: str = "data/db/content.db",
    report_path: str = "data/exports/report.json",
    csv_path: str = "data/exports/content.csv",
    after: str | None = None,
    before: str | None = None,
    dry_run: bool = False,
    reset_db: bool = False,
    max_queries: int | None = None,   # SerpAPI 검색 상한 override (None이면 settings 사용)
) -> dict:
    with open(settings_config, encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    date_range = dict(settings.get("date_range", {}))
    if after:
        date_range["after"] = after
    if before:
        date_range["before"] = before
    m = settings.get("matching", {})
    auto_save = m.get("auto_save_threshold", 0.8)
    review_th = m.get("review_threshold", 0.5)
    limits = settings.get("run_limits", {})
    budget_cfg = dict(settings.get("budget", {}))
    if max_queries is not None:
        budget_cfg["global_max_queries"] = max_queries
    elif "global_max_queries" not in budget_cfg:
        budget_cfg["global_max_queries"] = limits.get("max_queries_per_run")
    if "global_max_extracts" not in budget_cfg:
        budget_cfg["global_max_extracts"] = limits.get("max_extracts_per_run")

    registry = SiteRegistry.load(site_config)
    query_gen = QueryGenerator(registry, date_range)
    budget = Budget(budget_cfg)
    strategy_router = StrategyRouter()
    discovery = DiscoveryRouter(registry, query_gen, budget,
                                limits.get("max_urls_per_query", 5), settings)
    url_filter = UrlFilter(registry, date_range, settings.get("filtering", {}))

    policies = load_policies(taxonomy_config)

    if dry_run:
        return _dry_run(policies, strategy_router, discovery, url_filter, limits)

    extractor = ExtractorRouter(registry, settings)
    quality = QualityFilter(settings)
    matcher = _build_matcher(settings, auto_save, review_th)
    masker = BasicPIIMasker(settings.get("privacy", {}))
    dedup_threshold = settings.get("dedup", {}).get("event_hamming_threshold", 3)
    deduper = EventDeduper(dedup_threshold)
    store = Store(db_path, reset=reset_db)
    ctx = _Ctx(url_filter, extractor, quality, matcher, masker, deduper, store, budget,
               _dt.date.today().isoformat(), dedup_threshold,
               settings.get("privacy", {}).get("save_raw_text", True))
    ctx.artifact = ArtifactStore.from_settings(settings)

    for policy in policies:
        for subtype in policy.subtypes:
            for task in strategy_router.build_tasks(policy.taxonomy_lv2, subtype):
                _run_task(task, subtype, discovery, ctx)

    privacy = settings.get("privacy", {})
    include_raw = privacy.get("export_raw_text",
                              settings.get("export", {}).get("include_raw", False))
    n = export_csv(store, csv_path, include_raw=include_raw)
    report = build_report(store)
    report["csv_rows"] = n
    export_report(report, report_path)
    print_report(report)
    store.close()
    return report


class _Ctx:
    """subtype 실행에 필요한 컴포넌트 묶음."""
    def __init__(self, url_filter, extractor, quality, matcher, masker, deduper, store, budget,
                 collected_at, dedup_threshold, save_raw_text):
        self.url_filter = url_filter
        self.extractor = extractor
        self.quality = quality
        self.matcher = matcher
        self.masker = masker
        self.deduper = deduper
        self.store = store
        self.budget = budget
        self.collected_at = collected_at
        self.dedup_threshold = dedup_threshold
        self.save_raw_text = save_raw_text


def _run_task(task, subtype, discovery, ctx: _Ctx):
    taxonomy_lv2 = task.taxonomy_lv2
    candidates = discovery.discover(task, subtype)
    frontier = UrlFrontier(ctx.url_filter.registry)
    frontier.add_many(candidates)
    for cand in frontier.pending():
        cand.status = "discovered"
        ctx.store.save_candidate(cand)
        if frontier.is_reference(cand):
            cand.status, cand.filter_reason = "reference_only", "reference_page"
            ctx.store.save_candidate(cand)
            continue
        # Phase 4: URL-level 필터 (filter_mode 적용)
        r = ctx.url_filter.check(cand, subtype, taxonomy_lv2)
        if r.status == "fail":
            cand.status, cand.filter_reason = "url_filtered", r.reason
            ctx.store.save_candidate(cand)
            ctx.store.log_filter(cand.source_url, "url_filter", "fail", r.reason, taxonomy_lv2, subtype.name)
            continue

        # Phase 5–7: 추출 사다리
        if ctx.budget.take("extracts", taxonomy_lv2, task.collection_type) == 0:
            cand.status, cand.filter_reason = "pending_budget_exceeded", "extract_budget"
            ctx.store.save_candidate(cand)
            continue
        cand.status = "extracting"
        outcome = ctx.extractor.extract(cand, ctx.collected_at, task)
        if outcome.record is None:
            cand.status, cand.filter_reason = "extraction_failed", outcome.reason
            ctx.store.save_candidate(cand)
            ctx.store.log_filter(cand.source_url, "extract", "fail",
                                 f"{outcome.reason}; tried={outcome.tried}", taxonomy_lv2, subtype.name)
            continue
        cand.status = "extracted"
        ctx.store.save_candidate(cand)
        rec = outcome.record

        # Phase 8: 정제 + PII 마스킹 (raw/cleaned/masked). preservation_policy로 마스킹 범위 결정
        clean_record(rec, task.preservation_policy, ctx.masker)
        ctx.artifact.save_record(rec)

        # Phase 8.5: 추출로 확정된 날짜가 수집 기간 밖이면 버림 (URL 힌트 없이 통과한 건 차단).
        #            날짜 누락(published_at=None)은 기존대로 허용.
        if rec.published_at and not ctx.url_filter.in_range(rec.published_at):
            cand.status, cand.filter_reason = "out_of_date_range", f"published_at:{rec.published_at}"
            ctx.store.save_candidate(cand)
            ctx.store.log_filter(cand.source_url, "date_range", "fail",
                                 cand.filter_reason, taxonomy_lv2, subtype.name)
            continue

        # Phase 9: 품질 필터
        q = ctx.quality.check(rec)
        if q.status == "fail":
            cand.status, cand.filter_reason = "quality_failed", q.reason
            ctx.store.save_candidate(cand)
            ctx.store.log_filter(cand.source_url, "quality", "fail", q.reason, taxonomy_lv2, subtype.name)
            continue

        # Phase 10: 2단계 taxonomy matching
        match = ctx.matcher.match(taxonomy_lv2, subtype, rec)
        _copy_llm_usage(rec, cand)
        rec.taxonomy_lv2 = match.taxonomy_lv2
        rec.subtype = match.subtype
        rec.filter_reason = match.reason

        rec.filter_status = _classification_status(match, rec, task.thresholds, subtype.max_pii_risk)
        if rec.filter_status == "fail":
            rec.action = "discard"
            cand.status, cand.filter_reason = "matched_fail", "score_threshold_or_not_relevant"
            ctx.store.save_candidate(cand)
            ctx.store.log_filter(cand.source_url, "taxonomy_match", "fail",
                                 f"low_confidence:{match.confidence}", taxonomy_lv2, subtype.name)
            continue

        # 단계9: 동일 사건 near-dup (URL-dedup과 별도)
        rec.simhash = str(simhash(rec.masked_text or rec.body_text))
        rec.event_key = make_event_key(rec.title, rec.published_at, rec.site_name)
        rec.canonical_url = cand.canonical_url or cand.source_url
        db_dup = ctx.store.find_duplicate(rec, ctx.dedup_threshold)
        dup = ctx.deduper.check(subtype.name, int(rec.simhash), rec.event_key)
        if db_dup or dup:
            rec.duplicate_of = db_dup or dup
            cand.status, cand.filter_reason = "duplicate", f"duplicate_of:{rec.duplicate_of}"
            ctx.store.save_candidate(cand)
            ctx.store.log_filter(cand.source_url, "event_dedup", "fail",
                                 cand.filter_reason, taxonomy_lv2, subtype.name)
            continue
        ctx.deduper.add(subtype.name, int(rec.simhash), rec.event_key, rec.source_url)

        # Phase 11: accepted만 저장
        rec.action = "accepted"
        if not ctx.save_raw_text:
            rec.raw_text = ""
            rec.raw_comments = None
        ctx.store.save_content(rec)
        cand.status = "matched_pass"
        ctx.store.save_candidate(cand)
        ctx.store.log_filter(cand.source_url, "taxonomy_match", rec.filter_status,
                             rec.llm_escalation_reason or f"confidence:{match.confidence}",
                             taxonomy_lv2, subtype.name)




def _dry_run(policies, strategy_router, discovery, url_filter, limits) -> dict:
    """search + url_filter까지만. fetch/extract/store 없이 수집 예정 범위 프리뷰."""
    from collections import Counter
    domains: Counter = Counter()
    would_pass = 0
    for policy in policies:
        for subtype in policy.subtypes:
            for task in strategy_router.build_tasks(policy.taxonomy_lv2, subtype):
                for c in discovery.discover(task, subtype):
                    if url_filter.check(c, subtype, policy.taxonomy_lv2).status == "pass":
                        would_pass += 1
                        domains[c.domain] += 1
    report = {
        "dry_run": True,
        "would_extract": would_pass,
        "by_domain": dict(domains.most_common()),
        "run_limits": limits,
    }
    print_report(report)
    return report

