"""trend 모드 실행 — 디시/뉴스 트렌드 수집 → 정제 → LLM 단일 분류 → 저장."""
from __future__ import annotations

import datetime as _dt
import logging
import uuid
from collections import defaultdict

import yaml

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
    _candidate_in_window, _days_old,
    _parse_datetime, _published_in_window, _round_robin_candidates,
    _sample_by_topic_then_time, _target_stats,
)

log = logging.getLogger(__name__)

def run_trend(
    trend_config: str = "configs/trend_collection.yaml",
    taxonomy_config: str = "configs/taxonomy.yaml",
    site_config: str = "configs/site_policy.yaml",
    settings_config: str = "configs/crawler_settings.yaml",
    db_path: str = "data/db/content.db",
    report_path: str = "data/exports/report.json",
    csv_path: str = "data/exports/content.csv",
    dry_run: bool = False,
    reset_db: bool = False,
    overrides: dict | None = None,   # UI/호출자용: 기간·source enabled·안전 상한 덮어쓰기
    on_progress=None,                # on_progress(done, total) 콜백 (진행률 표시용)
) -> dict:
    """트렌드 주도 수집: 소스별 트렌딩/최신 수집 → 1차 basic → 2차 risk후보 → 단일 taxonomy 매핑."""
    with open(settings_config, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    with open(trend_config, encoding="utf-8") as f:
        trend = yaml.safe_load(f)
    if overrides:   # 설정파일 수정 없이 목표건수/소스 on-off 덮어쓰기
        trend.setdefault("collection", {}).update(overrides.get("collection", {}))
        trend.setdefault("sampling", {}).update(overrides.get("sampling", {}))
        for name, enabled in overrides.get("enabled", {}).items():
            trend.setdefault("sources", {}).setdefault(name, {})["enabled"] = enabled
        trend.setdefault("discovery_limits", {}).update(overrides.get("discovery_limits", {}))

    registry = SiteRegistry.load(site_config)
    policies = load_policies(taxonomy_config)
    valid_pairs, taxo_lines = build_taxonomy_index(policies)
    window = trend.get("collection", {})
    requested_lookback = max(
        1, int(window.get("lookback_days", window.get("exclude_older_than_days", 1)))
    )
    max_lookback = max(1, int(window.get("max_lookback_days", requested_lookback)))
    lookback_days = min(max_lookback, requested_lookback)
    now = _dt.datetime.now().astimezone()
    first_day = now.date() - _dt.timedelta(days=lookback_days - 1)
    cutoff = _dt.datetime.combine(first_day, _dt.time.min, tzinfo=now.tzinfo)
    srcs = trend.get("sources", {})
    ratios = trend.get("sampling_ratio", {})

    extractor = ExtractorRouter(registry, settings)   # 자체 Fetcher(robots/throttle) 보유

    # ── 최근 N일 제목·시각 후보 풀 구성. 본문 대상 quota와 탐색 상한을 분리한다. ──
    source_candidates = {}
    collection_targets = {}
    discovery_limits = trend.get("discovery_limits", {})
    dc = srcs.get("dcinside", {})
    if dc.get("enabled"):
        scan_cap = int(discovery_limits.get(
            "max_scan_candidates_by_source",
            discovery_limits.get("max_candidates_by_source", {}),
        ).get("dcinside", 5000))
        max_pages = int(discovery_limits.get(
            "max_pages_per_gallery", dc.get("max_pages", 1)))
        selected, seen = [], set()
        for page in range(1, max_pages + 1):
            raw = discover_dcinside_trend(
                dc.get("galleries", []), registry, extractor.fetcher,
                max_pages=1, start_page=page,
            )
            recent = [cand for cand in raw if _candidate_in_window(cand, cutoff)]
            _append_unique_candidates(recent, selected, seen, scan_cap)
            all_dated_old = raw and all(
                cand.published_at_hint and not _candidate_in_window(cand, cutoff) for cand in raw
            )
            if len(selected) >= scan_cap or not raw or all_dated_old:
                break
        source_candidates["dcinside"] = selected
        collection_targets["dcinside"] = _target_stats(len(selected), scan_cap)
    nr = srcs.get("news_rss", {})
    if nr.get("enabled"):
        scan_cap = int(discovery_limits.get(
            "max_scan_candidates_by_source",
            discovery_limits.get("max_candidates_by_source", {}),
        ).get("news_rss", 2000))
        raw = discover_news_trend(nr.get("feeds", []), registry, lookback_days)
        selected, seen = [], set()
        _append_unique_candidates(raw, selected, seen, scan_cap)
        source_candidates["news_rss"] = selected
        collection_targets["news_rss"] = _target_stats(len(selected), scan_cap)

    sampling = trend.get("sampling", {})
    daily_by_source = sampling.get("daily_quota_by_source", {})
    time_bucket_hours = int(sampling.get("time_bucket_hours", 4))
    engagement_ratio = float(sampling.get("engagement_ratio", 0.3))
    absolute_max = int(sampling.get("absolute_max_selected", 1000))
    store = None if dry_run else Store(db_path, reset=reset_db)
    processed_urls = store.processed_url_keys() if store else set()
    source_quota_bias: dict[str, float] = {}
    if store:
        rows = store.conn.execute(
            """
            SELECT COALESCE(source, source_type, 'unknown') AS src,
                   SUM(CASE WHEN action='accepted' THEN 1 ELSE 0 END) AS accepted,
                   SUM(CASE WHEN action IN ('accepted','discard') THEN 1 ELSE 0 END) AS processed
            FROM content_records
            WHERE COALESCE(is_supplementary,0)=0
              AND collection_phase = 1
              AND COALESCE(source, source_type, '') IN ('dcinside', 'news_rss')
            GROUP BY src
            """
        ).fetchall()
        for src, accepted, processed in rows:
            processed = int(processed or 0)
            accepted = int(accepted or 0)
            if processed < 20:
                source_quota_bias[str(src)] = 1.0
            else:
                # accepted rate가 높은 소스에 quota를 더 주고, discard가 많은 소스는 줄인다.
                source_quota_bias[str(src)] = max(0.25, min(1.75, 0.5 + (accepted / processed)))
    else:
        source_quota_bias = {}
    run_urls: set[str] = set()
    prefiltered_out, sampling_skipped = [], []
    enabled_sources = [s for s in source_candidates.keys() if source_candidates.get(s)]
    quota_total = sum(int(daily_by_source.get(s, 50 if s == "dcinside" else 30)) for s in enabled_sources)
    if enabled_sources and store and quota_total > 0:
        raw_weights = {
            s: source_quota_bias.get(s, 1.0)
            for s in enabled_sources
        }
        weight_sum = sum(raw_weights.values()) or float(len(enabled_sources))
        adjusted_daily = {
            s: max(0, round(quota_total * (raw_weights[s] / weight_sum)))
            for s in enabled_sources
        }
        # 반올림 오차는 가장 효율이 높은 소스에 몰아준다.
        diff = quota_total - sum(adjusted_daily.values())
        if diff:
            best_source = max(raw_weights, key=raw_weights.get)
            adjusted_daily[best_source] = max(0, adjusted_daily[best_source] + diff)
        daily_by_source = {**daily_by_source, **adjusted_daily}
    for source, pool in list(source_candidates.items()):
        fresh = []
        for cand in pool:
            key = cand.dedup_key()
            if key not in processed_urls and key not in run_urls:
                fresh.append(cand)
                run_urls.add(key)
        collection_targets[source]["already_processed"] = len(pool) - len(fresh)
        pool = fresh
        kept = []
        for cand in pool:
            pre = decide_candidate_action(cand)
            cand.filter_action = pre.filter_action
            cand.is_trend_seed = pre.is_trend_seed
            cand.filter_reason = pre.filter_reason
            if pre.filter_action == "keep":
                kept.append(cand)
            else:
                prefiltered_out.append(cand)
        quota = int(daily_by_source.get(source, 50 if source == "dcinside" else 30))
        # 시간대 배분은 두 소스 공통. 주제 축만 소스별로 다르다 —
        # dcinside는 갤러리 bucket(sampling_ratio), 뉴스는 RSS category(category_sampling).
        topic_ratios = (srcs.get("news_rss", {}).get("category_sampling", {})
                        if source == "news_rss" else ratios)
        selected = _sample_by_topic_then_time(
            kept, lookback_days, quota, topic_ratios, now.tzinfo,
            time_bucket_hours, engagement_ratio, absolute_max,
        )
        selected_keys = {c.dedup_key() for c in selected}
        sampling_skipped.extend(c for c in kept if c.dedup_key() not in selected_keys)
        source_candidates[source] = selected
        stats = collection_targets[source]
        stats["scanned"] = len(pool)
        stats["title_keep"] = len(kept)
        stats["prefilter_discard"] = len(pool) - len(kept)
        stats["selected"] = len(selected)

    # 한 소스가 먼저 전체 목표를 독점하지 않도록 원문 후보를 번갈아 처리한다.
    candidates = _round_robin_candidates(source_candidates)
    # 날짜만으로는 같은 날의 여러 실행을 구분할 수 없으므로, 1차도 실행 단위 ID를 남긴다.
    run_id = uuid.uuid4().hex[:12] if not dry_run else ""
    for cand in [*prefiltered_out, *sampling_skipped, *candidates]:
        cand.run_id = run_id
        cand.collection_phase = 1

    if dry_run:
        by_src: defaultdict = defaultdict(lambda: defaultdict(int))
        for c in candidates:
            source = c.meta.get("source", "?")
            by_src[source][c.meta.get("bucket", "?")] += 1
        report = {"dry_run": True, "mode": "trend",
                  "would_extract": sum(x["selected"] for x in collection_targets.values()),
                  "collection_targets": collection_targets,
                  "collection_window": {
                      "lookback_days": lookback_days, "cutoff": cutoff.isoformat(),
                  },
                  "by_source": {k: dict(v) for k, v in by_src.items()}}
        print_report(report)
        return report

    m = settings.get("matching", {})
    matcher = _build_matcher(settings, m.get("auto_save_threshold", 0.8), m.get("review_threshold", 0.5))
    masker = BasicPIIMasker(settings.get("privacy", {}))
    artifact = ArtifactStore.from_settings(settings)
    dedup_threshold = settings.get("dedup", {}).get("event_hamming_threshold", 3)
    deduper = EventDeduper(dedup_threshold)
    save_raw_text = settings.get("privacy", {}).get("save_raw_text", True)
    assert store is not None
    today = _dt.date.today()
    collected_at = today.isoformat()

    for cand in prefiltered_out:
        cand.status = "prefilter_discarded"
        store.save_candidate(cand)
        store.log_filter(
            cand.source_url, "relevance_prefilter",
            "seed" if cand.is_trend_seed else "fail", cand.filter_reason, "", "",
        )
    for cand in sampling_skipped:
        cand.status, cand.filter_reason = "sampling_skipped", "daily_time_quota"
        store.save_candidate(cand)

    llm_limit = int(trend.get("processing_limits", {}).get("max_llm_calls_per_day", 100))
    llm_calls_by_day: defaultdict = defaultdict(int)
    i = 0
    for cand in candidates:
        i += 1
        if on_progress:
            on_progress(i, len(candidates))
        source = cand.meta.get("source", "")
        stats = collection_targets.get(source)
        is_root = True
        cand.status = "discovered"
        store.save_candidate(cand)
        outcome = extractor.extract(cand, collected_at)
        if outcome.record is None:
            if is_root and stats:
                stats["extraction_failed"] += 1
            cand.status, cand.filter_reason = "extraction_failed", outcome.reason
            store.save_candidate(cand)
            store.log_filter(cand.source_url, "extract", "fail", outcome.reason, "", "")
            continue
        rec = outcome.record
        rec.run_id, rec.collection_phase = run_id, 1
        _apply_trend_meta(rec, cand.meta)
        clean_record(rec, _TREND_PRESERVATION, masker)
        artifact.save_record(rec)

        # 수집 윈도우 초과 → 1차 discard
        days_old = _days_old(rec.published_at, today)
        if rec.published_at and not _published_in_window(rec.published_at, cutoff):
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

        # ── 2차: keep 전체를 LLM으로 → 최종 accepted / discard ──
        published = _parse_datetime(rec.published_at or cand.published_at_hint, now.tzinfo)
        llm_day = published.date() if published else today
        limit_reached = bool(matcher.llm and llm_calls_by_day[llm_day] >= llm_limit)
        match = None
        if matcher.llm and not limit_reached:
            llm_calls_by_day[llm_day] += 1
            match = matcher.llm.classify(rec, policies, valid_pairs, taxo_lines)
        if match is None:                                # 미평가/기술 실패도 fail-closed discard
            if is_root and stats:
                stats["discard"] += 1
            if matcher.llm and not limit_reached and matcher.llm.last_usage.get("total"):
                matcher.llm._record_usage(rec)
                _copy_llm_usage(rec, cand)
            rec.action = rec.filter_action = "discard"
            rec.filter_reason = (
                "llm_daily_limit_reached" if limit_reached
                else (matcher.llm.last_error if matcher.llm else "llm_disabled") or "llm_failed"
            )
            _finalize(store, cand, rec, save_raw_text, store_content=False)
            continue
        _copy_llm_usage(rec, cand)
        # 최종 단일 경로 확정 후 로컬 threshold로 accepted/discard 판정
        rec.taxonomy_lv1 = match.taxonomy_lv1 or None
        rec.taxonomy_lv2 = match.taxonomy_lv2
        rec.category = rec.subtype = match.subtype   # subtype은 store/index/dedup 호환용 별칭
        rec.action = _trend_classification_action(match, rec, settings)
        rec.filter_status = "pass" if rec.action == "accepted" else "fail"
        rec.filter_reason = match.reason
        if rec.action == "discard":
            if is_root and stats:
                stats["discard"] += 1
            _finalize(store, cand, rec, save_raw_text, store_content=False)
            continue

        # accepted: taxonomy와 감사 근거를 저장
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

        if not save_raw_text:
            rec.raw_text, rec.raw_comments = "", None
        store.save_content(rec)
        if is_root and stats:
            stats[rec.action] += 1
        cand.status = f"trend_{rec.action}"
        store.save_candidate(cand)
        store.log_filter(cand.source_url, "trend_classify", rec.filter_status,
                         f"{rec.action}; conf={match.confidence}", rec.taxonomy_lv2 or "", rec.category or "")

    include_raw = settings.get("privacy", {}).get(
        "export_raw_text", settings.get("export", {}).get("include_raw", False))
    n = export_csv(store, csv_path, include_raw=include_raw)
    report = build_report(store)
    report["run_id"] = run_id
    report["csv_rows"] = n
    report["collection_targets"] = collection_targets
    report["collection_window"] = {
        "lookback_days": lookback_days,
        "cutoff": cutoff.isoformat(),
        "selected": sum(x.get("selected", 0) for x in collection_targets.values()),
        "accepted": sum(x["accepted"] for x in collection_targets.values()),
        "llm_calls_by_day": {str(day): count for day, count in sorted(llm_calls_by_day.items())},
    }
    export_report(report, report_path)
    print_report(report)
    store.close()
    return report
