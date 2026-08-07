"""gap_filling(2차 semantic) 모드 — 부족 taxonomy를 Tavily 발견으로 보강."""
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
    _allocate_buckets, _allocate_news_categories, _append_unique_candidates,
    _candidate_in_window, _days_old, _extract_links, _link_candidate,
    _published_in_window, _round_robin_candidates, _target_stats,
)

log = logging.getLogger(__name__)

def _load_phase2_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _apply_overrides(p2: dict, overrides: dict | None) -> dict:
    """UI 슬라이더 등이 config 위에 threshold/limit을 덮어쓴다(얕은 섹션 병합)."""
    if not overrides:
        return p2
    for section in ("rerank", "acceptance", "limits", "target_selection"):
        if section in overrides:
            p2.setdefault(section, {}).update(overrides[section])
    return p2


def _query_id(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def _default_provider():
    p = TavilyProvider()
    if p.available():
        return p
    log.warning("Tavily API 키 또는 SDK 없음 → MockTavilyProvider(오프라인/결정론적) 사용")
    return MockTavilyProvider()


def _phase2_llm(settings: dict):
    """classify/rerank용 LLMMatcher(있으면). matching.llm.enabled=false면 None."""
    m = settings.get("matching", {})
    return _build_matcher(settings, m.get("auto_save_threshold", 0.8),
                          m.get("review_threshold", 0.5)).llm


def preview_intents(config_path: str, db_path: str,
                    taxonomy_config: str = "configs/taxonomy.yaml",
                    overrides: dict | None = None) -> list[dict]:
    """stage1: 부족 LV2 랭킹 + LV2별 collection intent. API/fetch 없음."""
    p2 = _apply_overrides(_load_phase2_config(config_path), overrides)
    policies = load_policies(taxonomy_config)
    ts = p2.get("target_selection", {})
    targets = _coverage.resolve_targets(policies, ts)
    store = Store(db_path)
    cov = _coverage.weighted_coverage_by_lv2(store.conn, float(ts.get("review_weight", 0.0)))
    store.close()
    ranked = _coverage.rank_deficits(cov, targets, bool(ts.get("exclude_sufficient_lv2", True)))
    by_lv2 = {p.taxonomy_lv2: p for p in policies}
    out = []
    for r in ranked:
        pol = by_lv2.get(r["lv2"])
        if pol:
            out.append({**r, "intent": build_collection_intent(pol, p2)})
    return out


def preview_discovery(intent, provider=None, llm=None, rerank_cfg: dict | None = None) -> list[dict]:
    """stage2: provider로 후보 discovery + rerank. fetch/extract/저장 없음."""
    provider = provider or _default_provider()
    results = provider.search(intent)
    return [{"result": r, "rerank": rerank(r, intent, llm, rerank_cfg)} for r in results]


def _prov_stats() -> dict:
    return {k: 0 for k in (
        "candidate_count", "rerank_fetch_count", "extract_success_count",
        "accepted", "discard", "duplicate",
        "target_match", "korea_pass")} | {"llm_cost": 0.0}


def small_run(lv2s: list[str] | None, limit: int, config_path: str, db_path: str,
              taxonomy_config: str = "configs/taxonomy.yaml",
              site_config: str = "configs/site_policy.yaml",
              settings_config: str = "configs/crawler_settings.yaml",
              overrides: dict | None = None, provider=None,
              report_path: str | None = None, on_progress=None,
              discovery_cache: dict[str, list[dict]] | None = None) -> dict:
    """stage3: 선택 LV2들에 discovery→rerank→fetch/extract→clean→classify→판정→dedup→저장."""
    with open(settings_config, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    p2 = _apply_overrides(_load_phase2_config(config_path), overrides)
    policies = load_policies(taxonomy_config)
    valid_pairs, taxo_lines = build_taxonomy_index(policies)
    ts = p2.get("target_selection", {})
    limits = p2.get("limits", {})
    max_lv2 = int(limits.get("max_selected_lv2", 5))
    max_fetch_lv2 = int(limits.get("max_fetch_per_lv2", 20))
    max_total_fetch = min(int(limits.get("max_total_fetch", 60)), limit) if limit else int(limits.get("max_total_fetch", 60))
    max_classify = int(limits.get("max_total_llm_classify", 60))
    rerank_cfg = p2.get("rerank", {})

    registry = SiteRegistry.load(site_config)
    extractor = ExtractorRouter(registry, settings)
    quality = QualityFilter(settings)
    masker = BasicPIIMasker(settings.get("privacy", {}))
    artifact = ArtifactStore.from_settings(settings)
    matcher_llm = _phase2_llm(settings)
    dedup_threshold = settings.get("dedup", {}).get("event_hamming_threshold", 3)
    deduper = EventDeduper(dedup_threshold)
    comment_cfg = settings.get("extraction", {}).get("comments", {})
    save_raw_text = settings.get("privacy", {}).get("save_raw_text", True)
    store = Store(db_path)
    provider = provider or _default_provider()
    run_id = uuid.uuid4().hex[:12]
    collected_at = _dt.date.today().isoformat()

    # 목표/커버리지 (deficit 계산용)
    targets = _coverage.resolve_targets(policies, ts)
    initial_cov = _coverage.weighted_coverage_by_lv2(store.conn, 0.0)
    collected: dict = defaultdict(float)

    def deficit_of(lv2: str) -> float:
        return targets.get(lv2, 0.0) - (initial_cov.get(lv2, 0.0) + collected[lv2])

    intents = preview_intents(config_path, db_path, taxonomy_config, overrides)
    if lv2s:
        intents = [it for it in intents if it["lv2"] in set(lv2s)]
    intents = intents[:max_lv2]

    seen = {canonicalize_url(u) for u in store.existing_canonical_urls()}
    stats: dict = defaultdict(_prov_stats)
    total_fetch = total_classify = 0

    for it in intents:
        intent = it["intent"]
        target_lv2 = it["lv2"]
        fetched_lv2 = 0
        if discovery_cache is not None and target_lv2 in discovery_cache:
            entries = discovery_cache[target_lv2]
        else:
            entries = preview_discovery(intent, provider, matcher_llm, rerank_cfg)
        for entry in entries:
            if total_fetch >= max_total_fetch:
                break
            result, rr = entry["result"], entry["rerank"]
            cand = to_candidate(result, registry)
            cand.run_id = run_id
            cand.query_id = _query_id(intent.natural_language_query)
            cand.discovery_relevance_score = rr.discovery_relevance_score
            cand.korea_relevance_score = rr.korea_relevance_score
            prov = cand.discovery_provider or provider.name
            st = stats[prov]
            st["candidate_count"] += 1
            cand.status = "discovered"

            if rr.fetch_decision == "skip":
                cand.filter_reason = f"rerank_skip:{rr.reason}"[:200]
                cand.status = "rerank_skipped"
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "rerank", "skip", cand.filter_reason, target_lv2, "")
                continue
            key = cand.dedup_key()
            if key in seen or fetched_lv2 >= max_fetch_lv2:
                cand.status, cand.filter_reason = "duplicate_url", "seen_or_lv2_cap"
                store.save_candidate(cand)
                continue
            seen.add(key)

            st["rerank_fetch_count"] += 1
            fetched_lv2 += 1
            total_fetch += 1
            if on_progress:
                on_progress(total_fetch, max_total_fetch)

            outcome = extractor.extract(cand, collected_at)
            if outcome.record is None:
                cand.status, cand.filter_reason = "extraction_failed", outcome.reason
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "extract", "fail", outcome.reason, target_lv2, "")
                continue
            st["extract_success_count"] += 1
            rec = outcome.record
            rec.run_id, rec.collection_phase, rec.query_id = run_id, 2, cand.query_id
            rec.discovery_provider, rec.discovery_query = prov, intent.natural_language_query
            rec.discovery_relevance_score = rr.discovery_relevance_score
            rec.korea_relevance_score = rr.korea_relevance_score
            rec.taxonomy_lv2_candidate = target_lv2
            clean_record(rec, _TREND_PRESERVATION, masker, comment_cfg)
            artifact.save_record(rec)

            q = quality.check(rec)
            if q.status == "fail":
                cand.status, cand.filter_reason = "quality_failed", q.reason
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "quality", "fail", q.reason, target_lv2, "")
                continue

            # [12] LLM taxonomy remapping (masked 기준)
            match = None
            if matcher_llm and total_classify < max_classify:
                match = matcher_llm.classify(rec, policies, valid_pairs, taxo_lines)
                total_classify += 1
                _copy_llm_usage(rec, cand)
                st["llm_cost"] += rec.llm_estimated_cost_usd or 0.0
            if match is None:
                cand.status = "trend_discard"
                cand.filter_reason = getattr(matcher_llm, "last_error", "") or "llm_unavailable"
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "phase2_classify", "fail",
                                 cand.filter_reason, target_lv2, "")
                st["discard"] += 1
                continue

            rec.taxonomy_lv1 = match.taxonomy_lv1 or None
            rec.taxonomy_lv2 = match.taxonomy_lv2
            rec.category = rec.subtype = match.subtype
            action, reason = _phase2_adjudicate(match, rec, target_lv2, p2, deficit_of)
            if action == "accepted" and intent.force_review:
                action, reason = "discard", f"sensitive_requires_manual_review;{reason}"
            rec.action = action
            rec.filter_status = "pass" if action == "accepted" else "fail"
            rec.classification_source = match.source
            rec.classification_reason = rec.filter_reason = reason

            if action == "discard":
                cand.status, cand.filter_reason = "trend_discard", reason
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "phase2_classify", "fail", reason,
                                 rec.taxonomy_lv2 or "", rec.category or "")
                st["discard"] += 1
                continue

            # [15] near-dup
            rec.simhash = str(simhash(rec.masked_text or rec.body_text))
            rec.event_key = make_event_key(rec.title, rec.published_at, rec.site_name)
            rec.canonical_url = cand.canonical_url or cand.source_url
            group = rec.taxonomy_lv2 or "phase2"
            dup = store.find_duplicate(rec, dedup_threshold) or deduper.check(group, int(rec.simhash), rec.event_key)
            if dup:
                rec.duplicate_of = dup
                cand.status, cand.filter_reason = "duplicate", f"duplicate_of:{dup}"
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "event_dedup", "fail", cand.filter_reason,
                                 rec.taxonomy_lv2 or "", rec.category or "")
                st["duplicate"] += 1
                continue
            deduper.add(group, int(rec.simhash), rec.event_key, rec.source_url)
            _phase2_store(store, cand, rec, save_raw_text, target_lv2)
            collected[rec.taxonomy_lv2] += 1.0
            st[action] += 1
            if rec.taxonomy_lv2 == target_lv2:
                st["target_match"] += 1
            if (rec.korea_relevance_score or 0) >= float(p2.get("acceptance", {}).get("min_korea_relevance_score", 0.6)):
                st["korea_pass"] += 1

    report = build_report(store)
    report["mode"] = "targeted"
    report["run_id"] = run_id
    report["coverage"] = _phase2_coverage(targets, initial_cov, collected)
    report["provider_performance"] = _finalize_prov_stats(stats)
    if report_path:
        export_report(report, report_path)
    print_report(report)
    store.close()
    return report


def _phase2_coverage(targets, initial_cov, collected) -> dict:
    out = {}
    for lv2, target in targets.items():
        before = round(initial_cov.get(lv2, 0.0), 2)
        gained = round(collected.get(lv2, 0.0), 2)
        if before <= 0 and gained <= 0 and target <= 0:
            continue
        out[lv2] = {"target": target, "before": before, "collected": gained,
                    "after": round(before + gained, 2),
                    "shortfall": round(max(0.0, target - before - gained), 2)}
    return {k: out[k] for k in sorted(out, key=lambda x: out[x]["shortfall"], reverse=True)}


def _finalize_prov_stats(stats: dict) -> dict:
    out = {}
    for prov, s in stats.items():
        stored = s["accepted"]
        out[prov] = {
            **s,
            "llm_cost": round(s["llm_cost"], 6),
            "extract_success_rate": round(s["extract_success_count"] / s["rerank_fetch_count"], 3) if s["rerank_fetch_count"] else 0.0,
            "target_match_rate": round(s["target_match"] / stored, 3) if stored else 0.0,
            "korea_relevance_pass_rate": round(s["korea_pass"] / stored, 3) if stored else 0.0,
            "duplicate_rate": round(s["duplicate"] / s["extract_success_count"], 3) if s["extract_success_count"] else 0.0,
            "cost_per_accepted": round(s["llm_cost"] / s["accepted"], 6) if s["accepted"] else 0.0,
        }
    return out


def run_targeted(config: str = "configs/phase2_semantic_collection.yaml",
                 taxonomy_config: str = "configs/taxonomy.yaml",
                 site_config: str = "configs/site_policy.yaml",
                 settings_config: str = "configs/crawler_settings.yaml",
                 db_path: str = "data/db/content.db",
                 report_path: str = "data/exports/phase2_report.json",
                 dry_run: bool = False, overrides: dict | None = None) -> dict:
    """CLI full run. dry_run이면 stage1(intent 프리뷰)만."""
    if dry_run:
        intents = preview_intents(config, db_path, taxonomy_config, overrides)
        report = {"dry_run": True, "mode": "targeted", "deficits": [
            {"lv2": it["lv2"], "target": it["target"], "effective": it["effective"],
             "deficit": it["deficit"], "intent": it["intent"].natural_language_query}
            for it in intents
        ]}
        print_report(report)
        return report
    p2 = _load_phase2_config(config)
    max_total = int(p2.get("limits", {}).get("max_total_fetch", 60))
    lv2s = [it["lv2"] for it in preview_intents(config, db_path, taxonomy_config, overrides)]
    return small_run(lv2s, max_total, config, db_path, taxonomy_config, site_config,
                     settings_config, overrides=overrides, report_path=report_path)
