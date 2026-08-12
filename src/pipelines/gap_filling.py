"""gap_filling(2차 semantic) 모드 — 부족 taxonomy를 Tavily 발견으로 보강."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import sqlite3
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
from ..phase2.intent_builder import (
    build_collection_intent,
    missing_manual_intents,
    validate_collection_intent,
)
from ..phase2.provider import MockTavilyProvider, TavilyProvider, to_candidate
from ..phase2.reranker import rerank
from ..policy import load_policies
from ..keyword_discovery.query import QueryGenerator
from ..filtering.quality import QualityFilter
from ..filtering.relevance_filter import RELEVANCE_SIGNAL_TO_LV2, decide_candidate_action, decide_filter_action
from ..reporting.report import build_report, export_csv, export_report, print_report
from ..schema import ContentRecord, canonicalize_url
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

def _load_phase2_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _apply_overrides(p2: dict, overrides: dict | None) -> dict:
    """UI 슬라이더 등이 config 위에 threshold/limit을 덮어쓴다(얕은 섹션 병합)."""
    if not overrides:
        return p2
    for section in ("rerank", "acceptance", "adjudication", "limits", "target_selection"):
        if section in overrides:
            p2.setdefault(section, {}).update(overrides[section])
    return p2


def _query_id(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def _default_provider(p2: dict | None = None):
    options = ((p2 or {}).get("providers", {}).get("tavily", {}))
    p = TavilyProvider(options=options)
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
    missing = missing_manual_intents(policies, p2)
    if missing:
        log.warning("수동 intent 없음 %d/%d → taxonomy 자동 파생 사용: %s",
                    len(missing), len(policies), ", ".join(missing))
    ts = p2.get("target_selection", {})
    targets = _coverage.resolve_targets(policies, ts)
    store = Store(db_path)
    cov = _coverage.weighted_coverage_by_lv2(store.conn, float(ts.get("review_weight", 0.0)))
    blocked_domains = store.robots_disallowed_domains()
    store.close()
    ranked = _coverage.rank_deficits(cov, targets, bool(ts.get("exclude_sufficient_lv2", True)))
    by_lv2 = {p.taxonomy_lv2: p for p in policies}
    out = []
    for r in ranked:
        pol = by_lv2.get(r["lv2"])
        if not pol:
            continue
        intent = build_collection_intent(pol, p2)
        # 한 번 robots 정책으로 수집 불가가 확인된 소스는 다음 Tavily 검색부터 제외한다.
        intent.excluded_domains = list(dict.fromkeys(intent.excluded_domains + blocked_domains))
        check = validate_collection_intent(intent)
        if check["status"] == "blocked":   # 민감 LV2: 잘못된 쿼리는 한 번 호출되는 것도 사고다
            log.error("%s: 쿼리 차단 — %s", r["lv2"], " · ".join(check["warnings"]))
            continue
        out.append({**r, "intent": intent, "warnings": check["warnings"]})
    return out


def preview_discovery(intent, provider=None, llm=None, rerank_cfg: dict | None = None) -> list[dict]:
    """stage2: provider로 후보 discovery + rerank. fetch/extract/저장 없음."""
    provider = provider or _default_provider()
    results = provider.search(intent)
    return [{"result": r, "rerank": rerank(r, intent, llm, rerank_cfg)} for r in results]


def _relevance_first_entries(entries):
    """type 균형보다 관련성 높은 후보의 본문 수집을 우선한다."""
    return sorted(entries, key=lambda entry: (
        entry["rerank"].fetch_decision == "skip",
        entry["rerank"].fetch_decision != "fetch",
        -entry["rerank"].discovery_relevance_score,
    ))


def _prov_stats() -> dict:
    return {k: 0 for k in (
        "candidate_count", "rerank_fetch_count", "extract_success_count",
        "accepted", "candidate", "discard", "duplicate",
        "target_match", "korea_pass")} | {"llm_cost": 0.0}


def _query_stats() -> dict:
    """쿼리별로는 '이 검색어가 저장까지 갔는가'만 본다. provider 지표(비용·한국성)는 쿼리 단위로 안 갈린다."""
    return {k: 0 for k in ("candidate_count", "rerank_fetch_count", "extract_success_count",
                           "accepted", "candidate", "discard", "duplicate")}


def small_run(lv2s: list[str] | None, limit: int, config_path: str, db_path: str,
              taxonomy_config: str = "configs/taxonomy.yaml",
              site_config: str = "configs/site_policy.yaml",
              settings_config: str = "configs/crawler_settings.yaml",
              overrides: dict | None = None, provider=None,
              report_path: str | None = None, on_progress=None,
              discovery_cache: dict[str, list[dict]] | None = None,
              reference_db: str | None = None) -> dict:
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
    max_fetch_type = int(limits.get("max_fetch_per_type", max_fetch_lv2))
    max_total_fetch = min(int(limits.get("max_total_fetch", 60)), limit) if limit else int(limits.get("max_total_fetch", 60))
    max_classify = int(limits.get("max_total_llm_classify", 60))
    rerank_cfg = p2.get("rerank", {})
    verify_openai = bool(p2.get("adjudication", {}).get("openai_verification", False))

    registry = SiteRegistry.load(site_config)
    extractor = ExtractorRouter(registry, settings)
    quality = QualityFilter(settings)
    masker = BasicPIIMasker(settings.get("privacy", {}))
    artifact = ArtifactStore.from_settings(settings)
    # rerank LLM(fetch 전 애매밴드 1회 호출)과 본문 검수 LLM은 비용 성격이 다르다.
    # 검수를 꺼도 rerank 판정은 살려 헛fetch를 막는다.
    rerank_llm = _phase2_llm(settings)
    matcher_llm = rerank_llm if verify_openai else None
    dedup_threshold = settings.get("dedup", {}).get("event_hamming_threshold", 3)
    deduper = EventDeduper(dedup_threshold)
    save_raw_text = settings.get("privacy", {}).get("save_raw_text", True)
    store = Store(db_path)
    provider = provider or _default_provider(p2)
    run_id = uuid.uuid4().hex[:12]
    collected_at = _dt.date.today().isoformat()

    # 목표/커버리지 (deficit 계산용)
    targets = _coverage.resolve_targets(policies, ts)
    initial_cov = _coverage.weighted_coverage_by_lv2(store.conn, 0.0)
    collected: dict = defaultdict(float)
    lv1_by_lv2 = {policy.taxonomy_lv2: policy.taxonomy_lv1 for policy in policies}

    def deficit_of(lv2: str) -> float:
        return targets.get(lv2, 0.0) - (initial_cov.get(lv2, 0.0) + collected[lv2])

    intents = preview_intents(config_path, db_path, taxonomy_config, overrides)
    if lv2s:
        intents = [it for it in intents if it["lv2"] in set(lv2s)]
    intents = intents[:max_lv2]

    # scratch DB에 쓰더라도 본 DB에 이미 있는 URL은 다시 사가지 않는다.
    # (Tavily 검색 + 본문 fetch + LLM 분류가 모두 재지출된다)
    seen = {canonicalize_url(u) for u in store.existing_canonical_urls()}
    if reference_db and reference_db != db_path:
        ref = Store(reference_db)
        seen |= {canonicalize_url(u) for u in ref.existing_canonical_urls()}
        ref.close()
        log.info("중복 방지 참조 DB %s → 기준 URL %d건", reference_db, len(seen))
    stats: dict = defaultdict(_prov_stats)
    qstats: dict = defaultdict(_query_stats)  # 쿼리별 성과. 다음 라운드 쿼리 교체의 근거.
    qlv2: dict = {}
    total_fetch = total_classify = 0

    for it in intents:
        intent = it["intent"]
        target_lv2 = it["lv2"]
        fetched_lv2 = 0
        fetched_type: dict[str, int] = defaultdict(int)
        # 단일 type LV2(2_E·4_I·6_Q·6_R·6_S)는 per-type 상한이 곧 LV2 상한이 되어 수집량만 깎인다.
        # LV2 공통 쿼리의 빈 라벨은 type이 아니므로 세지 않는다.
        real_types = {t for t in intent.query_types.values() if t}
        type_cap = max_fetch_type if len(real_types) > 1 else max_fetch_lv2
        if discovery_cache is not None and target_lv2 in discovery_cache:
            entries = discovery_cache[target_lv2]
        else:
            entries = preview_discovery(intent, provider, rerank_llm, rerank_cfg)
        entries = _relevance_first_entries(entries)
        for entry in entries:
            if total_fetch >= max_total_fetch:
                break
            result, rr = entry["result"], entry["rerank"]
            cand = to_candidate(result, registry)
            cand.run_id = run_id
            cand.query_id = _query_id(result.query_or_intent)
            cand.subtype_candidate = intent.query_types.get(result.query_or_intent, "")
            cand.discovery_relevance_score = rr.discovery_relevance_score
            cand.korea_relevance_score = rr.korea_relevance_score
            prov = cand.discovery_provider or provider.name
            st = stats[prov]
            qs = qstats[result.query_or_intent]
            qlv2.setdefault(result.query_or_intent, target_lv2)
            st["candidate_count"] += 1
            qs["candidate_count"] += 1
            cand.status = "discovered"

            if rr.fetch_decision == "skip":
                cand.filter_reason = f"rerank_skip:{rr.reason}"[:200]
                cand.status = "rerank_skipped"
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "rerank", "skip", cand.filter_reason, target_lv2, "")
                continue
            key = cand.dedup_key()
            subtype = cand.subtype_candidate or "lv2_common"
            if key in seen or fetched_lv2 >= max_fetch_lv2 or fetched_type[subtype] >= type_cap:
                cand.status, cand.filter_reason = "duplicate_url", "seen_or_budget_cap"
                store.save_candidate(cand)
                continue
            seen.add(key)

            st["rerank_fetch_count"] += 1
            qs["rerank_fetch_count"] += 1
            fetched_lv2 += 1
            fetched_type[subtype] += 1
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
            qs["extract_success_count"] += 1
            rec = outcome.record
            rec.run_id, rec.collection_phase, rec.query_id = run_id, 2, cand.query_id
            rec.discovery_provider, rec.discovery_query = prov, result.query_or_intent
            rec.discovery_relevance_score = rr.discovery_relevance_score
            rec.korea_relevance_score = rr.korea_relevance_score
            rec.taxonomy_lv2_candidate = target_lv2
            rec.subtype_candidate = cand.subtype_candidate
            clean_record(rec, _TREND_PRESERVATION, masker)
            artifact.save_record(rec)

            q = quality.check(rec)
            if q.status == "fail":
                cand.status, cand.filter_reason = "quality_failed", q.reason
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "quality", "fail", q.reason, target_lv2, "")
                continue

            # [12] OpenAI 검수는 선택 사항. 끄면 Tavily 목표 taxonomy로만 미검수 후보를 저장한다.
            if not verify_openai:
                rec.taxonomy_lv1 = lv1_by_lv2.get(target_lv2)
                rec.taxonomy_lv2 = target_lv2
                rec.category = rec.subtype = cand.subtype_candidate or "-"
                action, reason = "candidate", "tavily_candidate_unverified"
                rec.classification_source = "tavily_unverified"
            else:
                match = None
                if matcher_llm and total_classify < max_classify:
                    match = matcher_llm.classify(
                        rec, policies, valid_pairs, taxo_lines,
                        broad_candidate=bool(p2.get("acceptance", {}).get("broad_candidate", False)),
                    )
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
                    qs["discard"] += 1
                    continue
                rec.taxonomy_lv1 = match.taxonomy_lv1 or None
                rec.taxonomy_lv2 = match.taxonomy_lv2
                rec.category = rec.subtype = match.subtype
                action, reason = _phase2_adjudicate(match, rec, target_lv2, p2, deficit_of)
                if action == "accepted" and intent.force_review:
                    action, reason = "discard", f"sensitive_requires_manual_review;{reason}"
                rec.classification_source = match.source

            rec.action = action
            rec.filter_status = "pass" if action == "accepted" else "review" if action == "candidate" else "fail"
            rec.classification_reason = rec.filter_reason = reason

            if action == "discard":
                cand.status, cand.filter_reason = "trend_discard", reason
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "phase2_classify", "fail", reason,
                                 rec.taxonomy_lv2 or "", rec.category or "")
                st["discard"] += 1
                qs["discard"] += 1
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
                qs["duplicate"] += 1
                continue
            deduper.add(group, int(rec.simhash), rec.event_key, rec.source_url)
            _phase2_store(store, cand, rec, save_raw_text, target_lv2)
            if action == "accepted":
                collected[rec.taxonomy_lv2] += 1.0
            st[action] += 1
            qs[action] += 1
            # 저장된 레코드(accepted + 미검수 candidate) 기준 비율. accepted만 세면
            # openai_verification=false일 때 분모가 0이라 모든 지표가 0으로 보인다.
            if rec.taxonomy_lv2 == target_lv2:
                st["target_match"] += 1
            if (rec.korea_relevance_score or 0) >= float(p2.get("acceptance", {}).get("min_korea_relevance_score", 0.6)):
                st["korea_pass"] += 1

    report = build_report(store)
    report["mode"] = "targeted"
    report["run_id"] = run_id
    report["coverage"] = _phase2_coverage(targets, initial_cov, collected)
    report["provider_performance"] = _finalize_prov_stats(stats)
    report["tavily_usage"] = provider.usage_summary()
    report["by_query"] = [
        {"query": q, "lv2": qlv2.get(q, ""), **s}
        for q, s in sorted(qstats.items(), key=lambda kv: kv[1]["accepted"], reverse=True)
    ]
    if report_path:
        export_report(report, report_path)
    print_report(report)
    store.close()
    return report


def verify_unverified_candidates(run_id: str, db_path: str, config_path: str,
                                 taxonomy_config: str = "configs/taxonomy.yaml",
                                 settings_config: str = "configs/crawler_settings.yaml",
                                 target_lv2: str | None = None, limit: int = 60,
                                 overrides: dict | None = None) -> dict:
    """기존 Tavily 미검수 후보의 저장 본문만 OpenAI로 재분류한다. Tavily/fetch는 재호출하지 않는다."""
    with open(settings_config, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    p2 = _apply_overrides(_load_phase2_config(config_path), overrides)
    policies = load_policies(taxonomy_config)
    valid_pairs, taxo_lines = build_taxonomy_index(policies)
    targets = _coverage.resolve_targets(policies, p2.get("target_selection", {}))
    store = Store(db_path)
    store.conn.row_factory = sqlite3.Row
    where = ["run_id=?", "collection_phase=2", "action='candidate'", "classification_source='tavily_unverified'"]
    params: list = [run_id]
    if target_lv2:
        where.append("taxonomy_lv2_candidate=?")
        params.append(target_lv2)
    rows = store.conn.execute(f"""
        SELECT content_id,source_url,domain,site_name,site_type,taxonomy_lv2_candidate,subtype_candidate,
               title,body_text,masked_text,collected_at,search_query,search_api,extractor,run_id
        FROM content_records WHERE {' AND '.join(where)} ORDER BY rowid LIMIT ?
    """, (*params, int(limit))).fetchall()
    matcher_llm = _phase2_llm(settings)
    if matcher_llm is None:
        store.close()
        return {"verified": 0, "accepted": 0, "discarded": 0, "errors": len(rows),
                "error": "openai_matcher_unavailable", "cost_usd": 0.0}

    coverage = _coverage.weighted_coverage_by_lv2(store.conn, 0.0)
    stats = {"verified": 0, "accepted": 0, "discarded": 0, "errors": 0, "cost_usd": 0.0}
    for row in rows:
        target = row["taxonomy_lv2_candidate"]
        rec = ContentRecord(
            source_url=row["source_url"], domain=row["domain"] or "", site_name=row["site_name"] or "",
            site_type=row["site_type"] or "", taxonomy_lv2_candidate=target,
            subtype_candidate=row["subtype_candidate"] or "", title=row["title"] or "",
            body_text=row["body_text"] or row["masked_text"] or "", masked_text=row["masked_text"] or "",
            collected_at=row["collected_at"] or "", search_query=row["search_query"] or "",
            search_api=row["search_api"] or "", extractor=row["extractor"] or "", content_id=row["content_id"],
            run_id=row["run_id"] or "", collection_phase=2,
        )
        match = matcher_llm.classify(
            rec, policies, valid_pairs, taxo_lines,
            broad_candidate=bool(p2.get("acceptance", {}).get("broad_candidate", False)),
        )
        if match is None:
            stats["errors"] += 1
            continue
        rec.taxonomy_lv1 = match.taxonomy_lv1 or None
        rec.taxonomy_lv2 = match.taxonomy_lv2
        rec.category = rec.subtype = match.subtype
        action, reason = _phase2_adjudicate(
            match, rec, target, p2, lambda lv2: targets.get(lv2, 0.0) - coverage.get(lv2, 0.0),
        )
        if action == "accepted" and p2.get("sensitive_overlay", {}).get(target, {}).get("force_review", False):
            action, reason = "discard", f"sensitive_requires_manual_review;{reason}"
        rec.action = action
        rec.filter_status = "pass" if action == "accepted" else "fail"
        rec.filter_reason = rec.classification_reason = reason
        rec.classification_source = match.source
        store.conn.execute("""
            UPDATE content_records SET taxonomy_lv1=?,taxonomy_lv2=?,subtype=?,category=?,action=?,
                filter_status=?,filter_reason=?,classification_source=?,classification_reason=?,
                taxonomy_relevance_score=?,taxonomy_fit_score=?,harmfulness_score=?,is_harmful=?,
                korea_relevance_score=?,contains_korean_context=?,concrete_context_score=?,evidence_spans=?,
                llm_model=?,llm_input_tokens=?,llm_cached_input_tokens=?,llm_output_tokens=?,llm_total_tokens=?,
                llm_estimated_cost_usd=? WHERE content_id=?
        """, (
            rec.taxonomy_lv1, rec.taxonomy_lv2, rec.subtype, rec.category, action, rec.filter_status,
            reason, rec.classification_source, reason, rec.taxonomy_relevance_score, rec.taxonomy_fit_score,
            rec.harmfulness_score, int(bool(rec.is_harmful)), rec.korea_relevance_score,
            int(bool(rec.contains_korean_context)), rec.concrete_context_score,
            json.dumps(rec.evidence_spans, ensure_ascii=False), rec.llm_model, rec.llm_input_tokens,
            rec.llm_cached_input_tokens, rec.llm_output_tokens, rec.llm_total_tokens,
            rec.llm_estimated_cost_usd, rec.content_id,
        ))
        candidate_status = "trend_accepted" if action == "accepted" else "trend_discard"
        store.conn.execute("""
            UPDATE url_candidates SET status=?,filter_reason=?,llm_model=?,llm_input_tokens=?,
                llm_cached_input_tokens=?,llm_output_tokens=?,llm_total_tokens=?,llm_estimated_cost_usd=?
            WHERE run_id=? AND source_url=? AND status='trend_candidate'
        """, (candidate_status, reason, rec.llm_model, rec.llm_input_tokens, rec.llm_cached_input_tokens,
              rec.llm_output_tokens, rec.llm_total_tokens, rec.llm_estimated_cost_usd, run_id, rec.source_url))
        store.conn.commit()
        store.log_filter(rec.source_url, "phase2_reverify", rec.filter_status, reason,
                         rec.taxonomy_lv2 or target, rec.category or "")
        stats["verified"] += 1
        stats["accepted" if action == "accepted" else "discarded"] += 1
        stats["cost_usd"] += rec.llm_estimated_cost_usd or 0.0
        if action == "accepted":
            coverage[rec.taxonomy_lv2] = coverage.get(rec.taxonomy_lv2, 0.0) + 1.0
    store.close()
    stats["cost_usd"] = round(stats["cost_usd"], 8)
    return stats


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
        stored = s["accepted"] + s["candidate"]   # DB에 남은 레코드 수
        out[prov] = {
            **s,
            "llm_cost": round(s["llm_cost"], 6),
            "extract_success_rate": round(s["extract_success_count"] / s["rerank_fetch_count"], 3) if s["rerank_fetch_count"] else 0.0,
            "target_match_rate": round(s["target_match"] / stored, 3) if stored else 0.0,
            "korea_relevance_pass_rate": round(s["korea_pass"] / stored, 3) if stored else 0.0,
            "duplicate_rate": round(s["duplicate"] / s["extract_success_count"], 3) if s["extract_success_count"] else 0.0,
            "cost_per_stored": round(s["llm_cost"] / stored, 6) if stored else 0.0,
        }
    return out


def run_targeted(config: str = "configs/targeted_collection.yaml",
                 taxonomy_config: str = "configs/taxonomy.yaml",
                 site_config: str = "configs/site_policy.yaml",
                 settings_config: str = "configs/crawler_settings.yaml",
                 db_path: str = "data/db/content.db",
                 report_path: str = "data/exports/phase2_report.json",
                 dry_run: bool = False, overrides: dict | None = None) -> dict:
    """CLI full run. dry_run이면 stage1(intent 프리뷰)만."""
    if dry_run:
        intents = preview_intents(config, db_path, taxonomy_config, overrides)
        report = {"dry_run": True, "mode": "targeted",
                  "missing_manual_intents": missing_manual_intents(
                      load_policies(taxonomy_config), _load_phase2_config(config)),
                  "deficits": [
                      {"lv2": it["lv2"], "target": it["target"], "effective": it["effective"],
                       "deficit": it["deficit"], "queries": it["intent"].queries,
                       "warnings": it["warnings"]}
                      for it in intents
                  ]}
        print_report(report)
        return report
    max_total = int(_load_phase2_config(config).get("limits", {}).get("max_total_fetch", 60))
    # lv2 선정은 small_run 안의 preview_intents가 그대로 한다. 여기서 미리 부르면 DB/커버리지 계산이 2회.
    return small_run(None, max_total, config, db_path, taxonomy_config, site_config,
                     settings_config, overrides=overrides, report_path=report_path)
