"""Phase 0→12 end-to-end. 실제 추출·2단계 분류·near-dup dedup을 비용-에스컬레이션으로 연결."""
from __future__ import annotations

import datetime as _dt
import logging
import re
from urllib.parse import urlparse

import yaml

from collections import defaultdict

from .clean import clean_record
from .dedup import EventDeduper, make_event_key, simhash
from .discovery import DiscoveryRouter
from .discovery.board import discover_dcinside_trend
from .discovery.rss import discover_news_trend
from .extract import ExtractorRouter
from .frontier import UrlFrontier
from .matcher import (LLMMatcher, RuleBasedMatcher, TieredMatcher, build_taxonomy_index,
                      confidence_bucket, risk_score_of, trend_score_of)
from .mask import BasicPIIMasker
from .policy import load_policies
from .query import QueryGenerator
from .quality import QualityFilter
from .relevance_filter import (
    RELEVANCE_SIGNAL_TO_LV2, decide_candidate_action, decide_filter_action,
)
from .report import build_report, export_csv, export_report, print_report
from .schema import UrlCandidate, canonicalize_url
from .site_registry import SiteRegistry
from .store import Store
from .strategy import Budget, StrategyRouter
from .url_filter import UrlFilter

# 트렌드 모드 마스킹 정책: 유해 표현 보존 + PII/credential 마스킹 (설계서 §12)
_TREND_PRESERVATION = {"preserve_harmful_expression": True, "mask_pii": True,
                       "restrict_actionable_detail": False, "mask_credentials": True}
# 최종 action → 레거시 filter_status. keep/discard=1차, accepted/excluded/pending=최종.
_ACTION_TO_STATUS = {"accepted": "pass", "excluded": "fail", "discard": "fail", "pending": "pending"}

log = logging.getLogger(__name__)


def run(
    taxonomy_config: str = "configs/taxonomy.yaml",
    site_config: str = "configs/site_policy.yaml",
    settings_config: str = "configs/crawler_settings.yaml",
    db_path: str = "data/content.db",
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
               settings.get("privacy", {}).get("save_raw_text", True),
               settings.get("extraction", {}).get("comments", {}))

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
                 collected_at, dedup_threshold, save_raw_text, comment_config):
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
        self.comment_config = comment_config


def _build_matcher(settings, auto_save, review_th):
    rule = RuleBasedMatcher(review_threshold=review_th)
    llm_cfg = settings.get("matching", {}).get("llm", {})
    llm = None
    if llm_cfg.get("enabled"):
        provider = llm_cfg.get("provider", "anthropic")
        default_model = "gpt-4o-mini" if provider == "openai" else "claude-haiku-4-5"
        llm = LLMMatcher(model=llm_cfg.get("model", default_model),
                         max_chars=llm_cfg.get("max_chars", 4000),
                         provider=provider,
                         pricing=llm_cfg.get("pricing", {}))
    return TieredMatcher(rule, llm, auto_save, review_th)


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
        clean_record(rec, task.preservation_policy, ctx.masker, ctx.comment_config)

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

        # Phase 11: 저장 (pass/review만)
        if not ctx.save_raw_text:
            rec.raw_text = ""
            rec.raw_comments = None
        ctx.store.save_content(rec)
        cand.status = "matched_pass" if rec.filter_status == "pass" else "matched_review"
        ctx.store.save_candidate(cand)
        ctx.store.log_filter(cand.source_url, "taxonomy_match", rec.filter_status,
                             rec.llm_escalation_reason or f"confidence:{match.confidence}",
                             taxonomy_lv2, subtype.name)


# review 저장 기준 (설계서 §13, 모든 taxonomy 공통 고정). pass 기준만 subtype.thresholds로 튜닝.
_REVIEW_FIT, _REVIEW_HARM = 0.60, 0.45


def _classification_status(match, rec, thresholds: dict, max_pii_risk: float) -> str:
    fit = rec.taxonomy_fit_score or 0
    harm = rec.harmfulness_score or 0
    val = rec.seed_source_value_score or 0
    pii = rec.pii_risk_score or 0
    t = thresholds or {}
    if (match.is_relevant
            and fit >= t.get("min_taxonomy_fit_score", 0.75)
            and harm >= t.get("min_harmfulness_score", 0.65)
            and val >= t.get("min_seed_source_value_score", 0.60)
            and pii <= max_pii_risk):
        return "pass"
    if match.is_relevant and fit >= _REVIEW_FIT and harm >= _REVIEW_HARM:
        return "review"
    return "fail"


def _trend_classification_action(match, rec, settings: dict) -> str:
    """LLM 점수를 로컬 기준으로 재판정한다: accepted | review | excluded."""
    if not match.is_relevant:
        return "excluded"
    m = settings.get("matching", {})
    confidence = match.confidence
    fit = rec.taxonomy_fit_score or 0
    harm = rec.harmfulness_score or 0
    concrete = rec.concrete_context_score or 0
    korea = rec.korea_relevance_score or 0
    if korea < float(m.get("min_korea_relevance", 0.3)):
        return "excluded"
    # LLM이 애매하다고 명시한 결과를 로컬 threshold가 excluded로 덮어쓰지 않는다.
    if rec.filter_status == "review":
        return "review"
    if rec.filter_status == "fail":
        return "excluded"
    if (confidence >= float(m.get("accepted_confidence", 0.75))
            and fit >= float(m.get("accepted_taxonomy_fit", 0.70))
            and harm >= float(m.get("accepted_harmfulness", 0.60))
            and concrete >= float(m.get("accepted_concrete_context", 0.50))):
        return "accepted"
    if (confidence >= float(m.get("review_confidence", 0.50))
            and fit >= float(m.get("review_taxonomy_fit", 0.50))):
        return "review"
    return "excluded"


# ────────────────────────── 트렌드 수집 모드 ──────────────────────────
def run_trend(
    trend_config: str = "configs/trend_collection.yaml",
    taxonomy_config: str = "configs/taxonomy.yaml",
    site_config: str = "configs/site_policy.yaml",
    settings_config: str = "configs/crawler_settings.yaml",
    db_path: str = "data/content.db",
    report_path: str = "data/exports/report.json",
    csv_path: str = "data/exports/content.csv",
    dry_run: bool = False,
    reset_db: bool = False,
    overrides: dict | None = None,   # UI/호출자용: target_by_source·enabled 덮어쓰기
    on_progress=None,                # on_progress(done, total) 콜백 (진행률 표시용)
) -> dict:
    """트렌드 주도 수집: 소스별 트렌딩/최신 수집 → 1차 basic → 2차 risk후보 → 단일 taxonomy 매핑."""
    with open(settings_config, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    with open(trend_config, encoding="utf-8") as f:
        trend = yaml.safe_load(f)
    if overrides:   # 설정파일 수정 없이 목표건수/소스 on-off 덮어쓰기
        trend.setdefault("target_by_source", {}).update(overrides.get("target_by_source", {}))
        for name, enabled in overrides.get("enabled", {}).items():
            trend.setdefault("sources", {}).setdefault(name, {})["enabled"] = enabled
        trend.setdefault("discovery_limits", {}).update(overrides.get("discovery_limits", {}))
        if overrides.get("comment_filter"):
            settings.setdefault("extraction", {}).setdefault("comments", {}).setdefault(
                "relevance_filter", {}
            ).update(overrides["comment_filter"])

    registry = SiteRegistry.load(site_config)
    policies = load_policies(taxonomy_config)
    valid_pairs, taxo_lines = build_taxonomy_index(policies)
    window = trend.get("collection", {})
    exclude_days = window.get("exclude_older_than_days", 14)
    targets = trend.get("target_by_source", {})
    srcs = trend.get("sources", {})
    target_policy = trend.get("target_policy", {})
    target_action = target_policy.get("action", "accepted")
    allow_cross_fill = bool(target_policy.get("allow_cross_source_fill", True))
    enabled_target = sum(
        int(value) for source, value in targets.items()
        if srcs.get(source, {}).get("enabled", False)
    )
    total_target = int(target_policy.get("total", enabled_target))
    ratios = trend.get("sampling_ratio", {})

    extractor = ExtractorRouter(registry, settings)   # 자체 Fetcher(robots/throttle) 보유

    # ── 목록 후보 풀 구성. 최종 accepted 목표는 상세 처리 루프에서 판정한다. ──
    source_candidates = {}
    collection_targets = {}
    discovery_limits = trend.get("discovery_limits", {})
    scan_multiplier = max(1, int(discovery_limits.get("max_scan_multiplier", 5)))
    dc = srcs.get("dcinside", {})
    if dc.get("enabled"):
        target = int(targets.get("dcinside", 0))
        hard_cap = int(discovery_limits.get("max_candidates_by_source", {}).get("dcinside", 2500))
        scan_cap = min(hard_cap, target * scan_multiplier) if target else hard_cap
        max_pages = int(discovery_limits.get(
            "max_pages_per_gallery", dc.get("max_pages", 1)))
        selected, seen = [], set()
        for page in range(1, max_pages + 1):
            raw = discover_dcinside_trend(
                dc.get("galleries", []), registry, extractor.fetcher,
                max_pages=1, start_page=page,
            )
            ordered = _allocate_buckets(raw, len(raw), ratios)
            _append_unique_candidates(ordered, selected, seen, scan_cap)
            if len(selected) >= scan_cap or not raw:
                break
        source_candidates["dcinside"] = selected
        collection_targets["dcinside"] = _target_stats(target, len(selected), scan_cap)
    nr = srcs.get("news_rss", {})
    if nr.get("enabled"):
        target = int(targets.get("news_rss", 0))
        hard_cap = int(discovery_limits.get("max_candidates_by_source", {}).get("news_rss", 1500))
        scan_cap = min(hard_cap, target * scan_multiplier) if target else hard_cap
        raw = discover_news_trend(nr.get("feeds", []), registry, exclude_days)
        ordered = _allocate_news_categories(
            raw, min(len(raw), scan_cap), nr.get("category_sampling", {})
        )
        selected, seen = [], set()
        _append_unique_candidates(ordered, selected, seen, scan_cap)
        source_candidates["news_rss"] = selected
        collection_targets["news_rss"] = _target_stats(target, len(selected), scan_cap)

    # 한 소스가 먼저 전체 목표를 독점하지 않도록 원문 후보를 번갈아 처리한다.
    candidates = _round_robin_candidates(source_candidates)

    if dry_run:
        by_src: defaultdict = defaultdict(lambda: defaultdict(int))
        for c in candidates:
            source = c.meta.get("source", "?")
            by_src[source][c.meta.get("bucket", "?")] += 1
            stats = collection_targets.get(source)
            if stats:
                stats["scanned"] += 1
                if decide_candidate_action(c).filter_action == "keep":
                    stats["title_keep"] += 1
                else:
                    stats["prefilter_discard"] += 1
        report = {"dry_run": True, "mode": "trend",
                  "would_extract": sum(x["title_keep"] for x in collection_targets.values()),
                  "collection_targets": collection_targets,
                  "collection_goal": {
                      "action": target_action, "total_target": total_target,
                      "allow_cross_source_fill": allow_cross_fill,
                  },
                  "by_source": {k: dict(v) for k, v in by_src.items()}}
        print_report(report)
        return report

    m = settings.get("matching", {})
    matcher = _build_matcher(settings, m.get("auto_save_threshold", 0.8), m.get("review_threshold", 0.5))
    masker = BasicPIIMasker(settings.get("privacy", {}))
    dedup_threshold = settings.get("dedup", {}).get("event_hamming_threshold", 3)
    deduper = EventDeduper(dedup_threshold)
    save_raw_text = settings.get("privacy", {}).get("save_raw_text", True)
    store = Store(db_path, reset=reset_db)
    today = _dt.date.today()
    collected_at = today.isoformat()

    # 본문 링크 follow (depth 1). candidates 리스트에 후속 링크를 append하며 함께 처리.
    fl = trend.get("follow_body_links", {})
    follow_enabled = fl.get("enabled", True)
    max_per_post = int(fl.get("max_per_post", 3))
    max_total_links = int(fl.get("max_total", 50))
    seen_urls = {c.dedup_key() for c in candidates}
    followed = 0

    i = 0
    for cand in candidates:               # 루프 중 candidates가 늘어나면 이어서 처리됨
        i += 1
        if on_progress:
            on_progress(i, len(candidates))
        source = cand.meta.get("source", "")
        stats = collection_targets.get(source)
        is_root = not cand.meta.get("is_supplementary") and cand.meta.get("depth", 0) == 0
        total_collected = sum(x.get(target_action, 0) for x in collection_targets.values())
        # accepted 전체 목표 달성 뒤 남은 원문은 요청하지 않는다. 보조 링크는 계속 처리한다.
        if is_root and total_target and total_collected >= total_target:
            continue
        if (is_root and not allow_cross_fill and stats and stats["target"]
                and stats.get(target_action, 0) >= stats["target"]):
            continue
        if is_root and stats:
            stats["scanned"] += 1
        cand.status = "discovered"
        store.save_candidate(cand)
        pre = decide_candidate_action(cand)
        cand.filter_action = pre.filter_action
        cand.is_trend_seed = pre.is_trend_seed
        cand.filter_reason = pre.filter_reason
        if pre.filter_action == "discard":
            if is_root and stats:
                stats["prefilter_discard"] += 1
            cand.status = "prefilter_discarded"
            store.save_candidate(cand)
            store.log_filter(
                cand.source_url, "relevance_prefilter",
                "seed" if pre.is_trend_seed else "fail",
                pre.filter_reason, "", "",
            )
            continue
        if is_root and stats:
            stats["title_keep"] += 1
        outcome = extractor.extract(cand, collected_at)
        if outcome.record is None:
            if is_root and stats:
                stats["extraction_failed"] += 1
            cand.status, cand.filter_reason = "extraction_failed", outcome.reason
            store.save_candidate(cand)
            store.log_filter(cand.source_url, "extract", "fail", outcome.reason, "", "")
            continue
        rec = outcome.record
        _apply_trend_meta(rec, cand.meta)
        clean_record(
            rec, _TREND_PRESERVATION, masker,
            settings.get("extraction", {}).get("comments", {}),
        )

        # 본문/댓글 링크는 독립 taxonomy 대상이 아니라 원문의 보조 콘텐츠로 보존한다.
        if rec.is_supplementary:
            rec.action = "supplementary"
            rec.filter_action = "keep"
            rec.filter_status = "pass"
            rec.filter_reason = "linked_context_collected"
            rec.canonical_url = cand.canonical_url or cand.source_url
            if not save_raw_text:
                rec.raw_text, rec.raw_comments = "", None
            store.save_content(rec)
            cand.status, cand.filter_reason = "supplementary_collected", rec.filter_reason
            store.save_candidate(cand)
            store.log_filter(cand.source_url, "linked_context", "pass", rec.filter_reason, "", "")
            continue

        # 수집 윈도우 초과 → 1차 discard
        days_old = _days_old(rec.published_at, today)
        if days_old is not None and days_old > exclude_days:
            if is_root and stats:
                stats["content_discard"] += 1
            rec.filter_action = rec.action = "discard"
            rec.filter_reason = f"out_of_window:{days_old}d"
            _finalize(store, cand, rec, save_raw_text, store_content=False)
            continue

        # 본문 gate는 감사용 신호만 추출한다. taxonomy 관련성은 제목 gate와 LLM이 판정한다.
        gate = decide_filter_action(rec)
        rec.is_taxonomy_relevant = gate.is_taxonomy_relevant
        rec.is_trend_seed = gate.is_trend_seed
        rec.risk_signals = gate.risk_signals
        rec.matched_keywords = gate.matched_keywords
        rec.negative_contexts = gate.negative_contexts
        rec.needs_comment_fallback = gate.needs_comment_fallback
        rec.is_risk_candidate = bool(gate.risk_signals)
        hard_discard = gate.filter_reason in {"link_only", "too_short", "advertisement"}
        rec.filter_action = "discard" if hard_discard else "keep"
        rec.filter_reason = gate.filter_reason
        if hard_discard:
            if is_root and stats:
                stats["content_discard"] += 1
            rec.action = "discard"
            _finalize(store, cand, rec, save_raw_text, store_content=False)
            continue

        # ── 2차: keep 전체를 LLM으로 → 최종 accepted / excluded / pending ──
        match = matcher.llm.classify(rec, policies, valid_pairs, taxo_lines) if matcher.llm else None
        if match is None:                                # LLM 미설정/실패 → 재처리 대상
            if is_root and stats:
                stats["pending"] += 1
            if matcher.llm and matcher.llm.last_usage.get("total"):
                matcher.llm._record_usage(rec)
                _copy_llm_usage(rec, cand)
            rec.action = "pending"
            rec.filter_reason = matcher.llm.last_error or "llm_failed"
            _finalize(store, cand, rec, save_raw_text, store_content=True)
            continue
        _copy_llm_usage(rec, cand)
        # 최종 단일 경로 확정 후 로컬 threshold로 accepted/review/excluded 판정
        rec.taxonomy_lv1 = match.taxonomy_lv1 or None
        rec.taxonomy_lv2 = match.taxonomy_lv2
        rec.category = rec.subtype = match.subtype   # subtype은 store/index/dedup 호환용 별칭
        rec.action = _trend_classification_action(match, rec, settings)
        rec.filter_status = {"accepted": "pass", "review": "review", "excluded": "fail"}[rec.action]
        rec.filter_reason = match.reason
        if rec.action == "excluded":
            if is_root and stats:
                stats["excluded"] += 1
            _finalize(store, cand, rec, save_raw_text, store_content=False)
            continue

        # accepted/review: taxonomy와 감사 근거를 저장
        signals = set(gate.risk_signals)
        rec.risk_signals = sorted(signals)
        rec.matched_keywords = match.matched_keywords or gate.matched_keywords
        rec.classification_source = match.source      # llm
        rec.classification_reason = match.reason
        primary_sigs = {s for s, lv in RELEVANCE_SIGNAL_TO_LV2.items() if lv == rec.taxonomy_lv2}
        rec.secondary_flags = sorted(signals - primary_sigs)   # primary 외 복합 위험
        rec.risk_score = risk_score_of(signals, rec.harmfulness_score)
        rec.trend_score = trend_score_of(days_old, rec.comment_count, rec.is_trending)
        rec.confidence = confidence_bucket(match.confidence)

        # near-dup (URL-dedup과 별도)
        rec.simhash = str(simhash(rec.masked_text or rec.body_text))
        rec.event_key = make_event_key(rec.title, rec.published_at, rec.site_name)
        rec.canonical_url = cand.canonical_url or cand.source_url
        group = rec.taxonomy_lv2 or "trend"
        dup = store.find_duplicate(rec, dedup_threshold) or deduper.check(group, int(rec.simhash), rec.event_key)
        if dup:
            if is_root and stats:
                stats["duplicate"] += 1
            rec.duplicate_of = dup
            cand.status, cand.filter_reason = "duplicate", f"duplicate_of:{dup}"
            store.save_candidate(cand)
            store.log_filter(cand.source_url, "event_dedup", "fail", cand.filter_reason,
                             rec.taxonomy_lv2 or "", rec.category or "")
            continue
        deduper.add(group, int(rec.simhash), rec.event_key, rec.source_url)

        # 보조 링크는 최종 usable 원문에서만 수집한다. 관련 없는 글의 링크 요청을 방지한다.
        if follow_enabled and cand.meta.get("depth", 0) == 0 and followed < max_total_links:
            for link, link_source in _extract_links(rec.raw_text, rec.raw_comments)[:max_per_post]:
                key = canonicalize_url(link)
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                candidates.append(_link_candidate(
                    link, registry, cand.meta.get("source", ""), cand.source_url, link_source,
                ))
                followed += 1
                if followed >= max_total_links:
                    break
        if not save_raw_text:
            rec.raw_text, rec.raw_comments = "", None
        store.save_content(rec)
        if is_root and stats:
            stats[rec.action] += 1
            if rec.action == target_action:
                stats["usable"] += 1
        cand.status = f"trend_{rec.action}"
        store.save_candidate(cand)
        store.log_filter(cand.source_url, "trend_classify", rec.filter_status,
                         f"{rec.action}; conf={match.confidence}", rec.taxonomy_lv2 or "", rec.category or "")

    include_raw = settings.get("privacy", {}).get(
        "export_raw_text", settings.get("export", {}).get("include_raw", False))
    n = export_csv(store, csv_path, include_raw=include_raw)
    report = build_report(store)
    report["csv_rows"] = n
    for stats in collection_targets.values():
        stats["shortfall"] = max(0, stats["target"] - stats["usable"])
    report["collection_targets"] = collection_targets
    total_accepted = sum(x["accepted"] for x in collection_targets.values())
    report["collection_goal"] = {
        "action": target_action,
        "total_target": total_target,
        "accepted": total_accepted,
        "review": sum(x["review"] for x in collection_targets.values()),
        "shortfall": max(0, total_target - total_accepted),
        "allow_cross_source_fill": allow_cross_fill,
    }
    export_report(report, report_path)
    print_report(report)
    store.close()
    return report


def _allocate_buckets(cands: list, target: int, ratios: dict) -> list:
    """버킷별 비중(sampling_ratio)에 따라 target 만큼 배분. 미달 시 leftover로 보충."""
    if not target:
        return cands
    by: defaultdict = defaultdict(list)
    for c in cands:
        by[c.meta.get("bucket", "latest")].append(c)
    picked, leftover = [], []
    for bucket, items in by.items():
        quota = round(target * ratios.get(bucket, 0)) if ratios else len(items)
        picked.extend(items[:quota])
        leftover.extend(items[quota:])
    if len(picked) < target:
        picked.extend(leftover[: target - len(picked)])
    return picked[:target]


def _append_unique_candidates(cands, selected, seen, scan_cap):
    """목록 후보를 URL 중복 없이 안전 상한까지 추가한다."""
    for cand in cands:
        key = cand.dedup_key()
        if key in seen:
            continue
        if len(selected) >= scan_cap:
            break
        seen.add(key)
        selected.append(cand)


def _round_robin_candidates(by_source: dict[str, list]) -> list:
    """소스별 후보를 한 건씩 번갈아 배치한다."""
    groups = list(by_source.values())
    return [group[i] for i in range(max(map(len, groups), default=0))
            for group in groups if i < len(group)]


def _target_stats(target, available, scan_cap):
    stats = {
        "target": target,
        "available": available,
        "scanned": 0,
        "title_keep": 0,
        "prefilter_discard": 0,
        "content_discard": 0,
        "extraction_failed": 0,
        "excluded": 0,
        "pending": 0,
        "duplicate": 0,
        "accepted": 0,
        "review": 0,
        "usable": 0,
        "shortfall": target,
        "scan_cap": scan_cap,
    }
    return stats


def _allocate_news_categories(cands: list, target: int, ratios: dict) -> list:
    """URL 중복을 제거하고 RSS category별 목표 비율로 후보를 배분한다."""
    unique = list({c.dedup_key(): c for c in cands}.values())
    if not target:
        return unique
    by: defaultdict = defaultdict(list)
    for cand in unique:
        by[cand.meta.get("category_name", "")].append(cand)
    picked, leftover = [], []
    for category, items in by.items():
        quota = round(target * ratios.get(category, 0)) if ratios else len(items)
        picked.extend(items[:quota])
        leftover.extend(items[quota:])
    if len(picked) < target:
        picked.extend(leftover[:target - len(picked)])
    return picked[:target]


def _apply_trend_meta(rec, meta: dict) -> None:
    rec.source = meta.get("source", "")
    rec.source_type = meta.get("source_type", "")
    rec.board_name = meta.get("board_name", "")
    rec.category_name = meta.get("category_name", "")
    rec.view_count = meta.get("view_count")
    rec.comment_count = meta.get("comment_count")
    rec.like_count = meta.get("like_count")
    rec.is_trending = bool(meta.get("is_trending", False))
    rec.collection_type = meta.get("bucket") or rec.collection_type   # 스펙: 버킷(trending/latest/rss)
    rec.crawl_status = "success"


def _copy_llm_usage(rec, cand) -> None:
    """excluded 콘텐츠까지 전체 사용량을 집계할 수 있도록 후보에도 usage를 기록한다."""
    for name in (
        "llm_model", "llm_input_tokens", "llm_cached_input_tokens",
        "llm_output_tokens", "llm_total_tokens", "llm_estimated_cost_usd",
    ):
        setattr(cand, name, getattr(rec, name, 0))


def _finalize(store, cand, rec, save_raw_text, store_content: bool) -> None:
    """탈락/보류 레코드 마무리. pending처럼 재처리에 원문이 필요할 때만 content_records에 저장."""
    rec.filter_status = _ACTION_TO_STATUS.get(rec.action, "fail")
    rec.canonical_url = cand.canonical_url or cand.source_url
    if store_content:
        if not save_raw_text:
            rec.raw_text, rec.raw_comments = "", None
        store.save_content(rec)
    cand.filter_action = rec.filter_action
    cand.status, cand.filter_reason = f"trend_{rec.action}", rec.filter_reason
    store.save_candidate(cand)
    store.log_filter(cand.source_url, "relevance_filter", rec.filter_status, rec.filter_reason, "", "")


_URL_RE = re.compile(r'https?://[^\s"\'<>)\]}]+')
_SKIP_LINK_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".mp3", ".pdf")


def _extract_links(text: str | None, comments: list | None) -> list[tuple[str, str]]:
    """본문+댓글 링크와 출처를 추출. 이미지·중복은 제외(동영상은 후보 단계에서 discard 기록)."""
    out, seen = [], set()
    chunks = [(text or "", "body")] + [(comment, "comment") for comment in (comments or [])]
    for chunk, link_source in chunks:
        for u in _URL_RE.findall(chunk):
            u = u.rstrip(".,)]}\"'")
            if u.lower().endswith(_SKIP_LINK_EXT) or u in seen:
                continue
            seen.add(u)
            out.append((u, link_source))
    return out


def _link_candidate(url: str, registry, parent_source: str,
                    parent_source_url: str, link_source: str) -> UrlCandidate:
    """본문 링크 → 후속 UrlCandidate(depth 1). 도메인으로 site_type 추론."""
    domain = urlparse(url).netloc.lower()
    info = registry.lookup(domain)
    st = "news" if info.site_type == "news" else (
        "community" if info.site_type in ("community", "dynamic") else info.site_type)
    cand = UrlCandidate(url, domain, f"link_from:{parent_source}", "in_body_link", "", "",
                        canonical_url=url, site_name=info.site_name, site_type=info.site_type,
                        collection_type="link", discovery_method="in_body_link",
                        parent_source_url=parent_source_url, link_source=link_source,
                        is_supplementary=True)
    cand.meta = {"source": info.site_name or domain, "source_type": st,
                 "board_name": "in_body_link", "bucket": "link", "is_trending": False, "depth": 1}
    return cand


def _days_old(published_at: str | None, today) -> int | None:
    if not published_at:
        return None
    try:
        d = _dt.date.fromisoformat(published_at[:10])
    except ValueError:
        return None
    return (today - d).days


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
