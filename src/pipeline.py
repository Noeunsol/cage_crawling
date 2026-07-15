"""Phase 0→12 end-to-end 연결. mock 데이터로 실제 파이프라인을 한 번에 실행."""
from __future__ import annotations

import datetime as _dt
import logging

import yaml

from .clean import clean_record
from .collection import CollectionStrategyRouter
from .extract import ExtractorRouter
from .frontier import UrlFrontier
from .matcher import RuleBasedMatcher
from .policy import load_policies
from .query import QueryGenerator
from .quality import QualityFilter
from .report import build_report, export_csv, export_report, print_report
from .site_registry import SiteRegistry
from .store import Store
from .url_filter import UrlFilter

# 기본 수집 전략 (설정 없으면 이 순서). seed_expansion/trend은 미구현이라 제외.
_DEFAULT_STRATEGIES = ["keyword", "semantic", "site_sampling"]

log = logging.getLogger(__name__)


def run(
    taxonomy_config: str = "configs/taxonomy_policy.yaml",
    site_config: str = "configs/site_policy.yaml",
    settings_config: str = "configs/crawler_settings.yaml",
    db_path: str = "data/content.db",
    report_path: str = "data/exports/report.json",
    csv_path: str = "data/exports/content.csv",
    after: str | None = None,
    before: str | None = None,
) -> dict:
    with open(settings_config, encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    # 기본값은 crawler_settings.yaml, CLI 인자가 있으면 덮어씀
    date_range = dict(settings.get("date_range", {}))
    if after:
        date_range["after"] = after
    if before:
        date_range["before"] = before
    m = settings.get("matching", {})
    auto_save = m.get("auto_save_threshold", 0.8)
    review_th = m.get("review_threshold", 0.5)

    enabled_strategies = settings.get("collection", {}).get("enabled", _DEFAULT_STRATEGIES)

    registry = SiteRegistry.load(site_config)
    query_gen = QueryGenerator(registry, date_range)
    collector = CollectionStrategyRouter(enabled_strategies, registry, query_gen)
    url_filter = UrlFilter(registry, date_range)
    extractor = ExtractorRouter(registry)
    quality = QualityFilter(settings)
    matcher = RuleBasedMatcher(review_threshold=review_th)
    store = Store(db_path)
    collected_at = _dt.date.today().isoformat()

    policies = load_policies(taxonomy_config)
    log.info("enabled policies: %d", len(policies))

    for policy in policies:
        for subtype in policy.subtypes:
            _run_subtype(
                policy.taxonomy_lv2, subtype, collector, url_filter, extractor,
                quality, matcher, store, collected_at, auto_save, review_th,
            )

    report = build_report(store)
    export_report(report, report_path)
    n = export_csv(store, csv_path)
    report["csv_rows"] = n
    print_report(report)
    log.info("CSV %d rows → %s", n, csv_path)
    store.close()
    return report


def _run_subtype(taxonomy_lv2, subtype, collector, url_filter, extractor, quality,
                 matcher, store, collected_at, auto_save, review_th):
    # Phase 2: 하이브리드 수집(keyword/semantic/site_sampling) → Phase 3: frontier
    candidates = collector.collect(taxonomy_lv2, subtype)
    frontier = UrlFrontier(url_filter.registry)
    frontier.add_many(candidates)

    for cand in frontier.pending():
        # Phase 4: URL-level 필터
        r = url_filter.check(cand, subtype)
        if r.status == "fail":
            cand.status = "filtered_out"
            store.log_filter(cand.source_url, "url_filter", "fail", r.reason, taxonomy_lv2, subtype.name)
            continue

        # Phase 5–7: 추출
        cand.status = "extracting"
        try:
            rec = extractor.extract(cand, collected_at)
        except Exception as e:  # 추출 실패 → url_candidates + filter_logs
            cand.status = "failed"
            store.save_candidate(cand)
            store.log_filter(cand.source_url, "extract", "fail", str(e), taxonomy_lv2, subtype.name)
            continue
        cand.status = "extracted"
        store.save_candidate(cand)

        # Phase 8: 정제 + PII 마스킹
        clean_record(rec)

        # Phase 9: 품질 필터
        q = quality.check(rec)
        if q.status == "fail":
            store.log_filter(cand.source_url, "quality", "fail", q.reason, taxonomy_lv2, subtype.name)
            continue

        # Phase 10: taxonomy matching → 저장 정책
        match = matcher.match(taxonomy_lv2, subtype, rec)
        rec.taxonomy_lv2 = match.taxonomy_lv2
        rec.subtype = match.subtype

        if match.confidence >= auto_save:
            rec.filter_status = "pass"
        elif match.confidence >= review_th:
            rec.filter_status = "review"
        else:
            store.log_filter(cand.source_url, "taxonomy_match", "fail",
                             f"low_confidence:{match.confidence}", taxonomy_lv2, subtype.name)
            continue

        # Phase 11: 저장 (pass/review만)
        rec.filter_reason = match.reason
        store.save_content(rec)
        store.log_filter(cand.source_url, "taxonomy_match", rec.filter_status,
                         f"confidence:{match.confidence}", taxonomy_lv2, subtype.name)
