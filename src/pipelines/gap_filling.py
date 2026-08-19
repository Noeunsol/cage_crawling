"""gap_filling(2차 semantic) 모드 — 부족 taxonomy를 Tavily 발견으로 보강."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import replace

import yaml

from ..reporting import coverage as _coverage
from ..clean import clean_record
from ..storage.dedup import EventDeduper, make_event_key, simhash
from ..extract import ExtractorRouter
from ..classify.matcher import build_taxonomy_index
from ..artifact_store import ArtifactStore
from ..phase2.intent_builder import (
    build_collection_intent,
    missing_manual_intents,
    validate_collection_intent,
)
from ..phase2 import acceptance as _acceptance
from ..phase2 import query_planner as _qp
from ..phase2 import source_router as _router
from ..phase2.provider import MockTavilyProvider, SerpApiProvider, TavilyProvider, to_candidate
from ..phase2.reranker import rerank
from ..policy import load_policies
from ..filtering.quality import QualityFilter
from ..reporting.report import build_report, export_report, print_report
from ..schema import ContentRecord, canonicalize_url
from ..site_registry import SiteRegistry
from ..storage.store import Store
from .persist import _phase2_store
from .stages import _build_matcher, _copy_llm_usage, _TREND_PRESERVATION
from .taxonomy_adjudication import _phase2_adjudicate

log = logging.getLogger(__name__)

def _load_phase2_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _apply_overrides(p2: dict, overrides: dict | None) -> dict:
    """UI 슬라이더 등이 config 위에 threshold/limit을 덮어쓴다(얕은 섹션 병합)."""
    if not overrides:
        return p2
    for section in ("rerank", "acceptance", "adjudication", "limits", "target_selection",
                    "query_planner", "default_acceptance"):
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


def _provider_for_name(name: str, p2: dict):
    """taxonomy plan의 provider 이름을 기존 discovery provider로 연결한다."""
    if name == "serpapi":
        serpapi = p2.get("serpapi", {})
        return SerpApiProvider(serpapi.get("provider", {}), serpapi.get("rules_by_lv2", {}))
    return _default_provider(p2)


def _type_coverage(conn: sqlite3.Connection, review_weight: float) -> dict[tuple[str, str], float]:
    rows = conn.execute("""
        SELECT taxonomy_lv2, COALESCE(NULLIF(category,''), subtype), action, COUNT(*)
        FROM content_records
        WHERE action IN ('accepted','review') AND COALESCE(is_supplementary,0)=0
          AND taxonomy_lv2 IS NOT NULL AND taxonomy_lv2!=''
        GROUP BY taxonomy_lv2, COALESCE(NULLIF(category,''), subtype), action
    """).fetchall()
    coverage: dict[tuple[str, str], float] = {}
    for lv2, subtype, action, count in rows:
        key = (lv2, subtype or "-")
        coverage[key] = coverage.get(key, 0.0) + count * (1.0 if action == "accepted" else review_weight)
    return coverage


def preview_taxonomy_plan(config_path: str, db_path: str,
                          taxonomy_config: str = "configs/taxonomy.yaml") -> list[dict]:
    """taxonomy-first 2차 실행 계획: LV2·type 부족분과 provider 순서를 한 번에 계산한다."""
    p2 = _load_phase2_config(config_path)
    policies = load_policies(taxonomy_config)
    plan_cfg = p2.get("taxonomy_collection_plan", {})
    defaults = plan_cfg.get("defaults", {})
    review_weight = float(defaults.get("review_weight", p2.get("target_selection", {}).get("review_weight", 0.0)))
    default_target = float(defaults.get("target_count", p2.get("target_selection", {}).get("min_accepted_per_lv2", 30)))
    routes = p2.get("collection_provider_routing", {})
    news_lv2s = set((routes.get("news_tavily", {}) or {}).get("lv2s", []))
    plans = plan_cfg.get("plans", {})
    store = Store(db_path)
    lv2_coverage = _coverage.weighted_coverage_by_lv2(store.conn, review_weight)
    type_coverage = _type_coverage(store.conn, review_weight)
    store.close()
    output = []
    for policy in policies:
        lv2 = policy.taxonomy_lv2
        plan = plans.get(lv2, {})
        target = float(plan.get("target_count", default_target))
        provider_order = plan.get("provider_order") or (["tavily", "serpapi"] if lv2 in news_lv2s else ["serpapi", "tavily"])
        type_overrides = plan.get("types", {})
        type_names = [subtype.name for subtype in policy.subtypes] or ["-"]
        per_type_target = target / len(type_names)
        types = []
        for subtype in type_names:
            type_target = float((type_overrides.get(subtype, {}) or {}).get("target_count", per_type_target))
            effective = round(type_coverage.get((lv2, subtype), 0.0), 3)
            types.append({"type": subtype, "target": type_target, "effective": effective,
                          "shortfall": round(max(0.0, type_target - effective), 3),
                          "provider_order": (type_overrides.get(subtype, {}) or {}).get("provider_order", provider_order)})
        effective = round(lv2_coverage.get(lv2, 0.0), 3)
        output.append({"lv2": lv2, "lv1": policy.taxonomy_lv1, "target": target, "effective": effective,
                       "shortfall": round(max(0.0, target - effective), 3), "provider_order": provider_order,
                       "types": sorted(types, key=lambda row: row["shortfall"], reverse=True)})
    return output  # taxonomy.yaml의 LV2 선언 순서 = UI·실행 선택 순서


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


def _rerank_entries(results, intent, llm=None, rerank_cfg: dict | None = None,
                    plan=None, source=None) -> list[dict]:
    """discovery 결과를 공통 엔트리 형식으로. 고정 intent 경로와 계획 경로가 이걸 공유한다."""
    return [
        {"result": r, "rerank": rerank(r, intent, llm, rerank_cfg),
         "plan": plan(r) if callable(plan) else plan, "source": source}
        for r in results
    ]


def preview_discovery(intent, provider=None, llm=None, rerank_cfg: dict | None = None) -> list[dict]:
    """stage2: provider로 후보 discovery + rerank. fetch/extract/저장 없음."""
    provider = provider or _default_provider()
    return _rerank_entries(provider.search(intent), intent, llm, rerank_cfg)


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


def _seed_signals(store: Store, lv2: str, limit: int) -> list[dict]:
    """Query Planner 입력용 최근 seed. 본문은 넘기지 않는다(제목·날짜·출처만)."""
    return [
        {"title": title, "published_at": published_at, "site_name": site_name, "source_url": url}
        for title, published_at, site_name, url in store.conn.execute(
            """SELECT title, published_at, site_name, source_url FROM content_records
               WHERE taxonomy_lv2=? AND action='accepted'
               ORDER BY COALESCE(published_at, collected_at) DESC LIMIT ?""", (lv2, limit))
    ]


def _plans_for(store: Store, lv2: str, strategy: dict, p2: dict, planner, definition: str,
               today, extra_seeds: list[dict] | None = None, query_kind: str = "") -> list[_qp.QueryPlan]:
    """계획 생성 또는 재사용. 재사용이면 OpenAI를 호출하지 않는다."""
    cfg = p2.get("query_planner", {})
    seeds = _seed_signals(store, lv2, int(cfg.get("max_seed_items", 20))) + list(extra_seeds or [])
    cached = _qp.latest_generation(store.load_query_plans(lv2))
    if not (query_kind or _qp.should_regenerate(cached, seeds, cfg, today)):
        # 재사용은 저장하지 않는다. 다시 쓰면 created_at이 갱신돼 max_age_days가 영원히 안 온다.
        return _qp.plans_from_rows(cached)
    max_total = int(cfg.get("max_queries_per_lv2", 12))
    plans = planner.plan(lv2, definition, strategy, seeds,
                         _qp.low_performing_queries(cached), query_kind)
    if not plans:
        manual = (p2.get("collection_intents_by_lv2", {}) or {}).get(lv2, {})
        plans = _qp.apply_budgets(
            _qp.fallback_plans(lv2, strategy, manual.get("queries_by_type", {}),
                               manual.get("include_by_type", {})),
            strategy, max_total)
    if plans:
        store.save_query_plans(
            [p.to_row(_qp.seed_fingerprint(seeds), today.isoformat()) for p in plans])
    return plans


def _strategy_entries(plans, strategy: dict, lv2: str, ctx, rerank_llm, rerank_cfg,
                      methods: set[str] | None = None) -> list[dict]:
    """source별 discovery → rerank. plan provenance를 엔트리에 붙여 후보로 옮긴다."""
    entries: list[dict] = []
    for source in sorted(strategy.get("sources") or [], key=lambda s: s.get("priority", 99)):
        if methods is not None and source.get("method") not in methods:
            continue
        source_plans = [p for p in plans if p.source_id == source["id"]]
        results = _router.discover(source_plans, source, strategy, lv2, ctx)
        if not results:
            continue
        intent = _router._intent_for(source_plans, strategy, lv2, 10)
        entries += _rerank_entries(
            results, intent, rerank_llm, rerank_cfg, source=source,
            # SerpAPI는 원문 query 앞뒤에 site:·suffix를 붙이므로 포함 관계로 되찾는다.
            plan=lambda r, sp=source_plans: next((p for p in sp if p.query in r.query_or_intent), None),
        )
    return entries


def _staged_entries(rounds):
    """라운드를 지연 평가한다. 2라운드 계획은 1라운드 저장이 끝난 뒤에 만들어져야 한다."""
    for make_entries in rounds:
        yield from _relevance_first_entries(make_entries())


_OFFICIAL = {"official_board"}


def _strategy_rounds(store, lv2, strategy, p2, planner, definition, today, ctx,
                     rerank_llm, rerank_cfg, official_seeds: list[dict]):
    """official_seed 모드는 2-hop: 공식기관 원문 수집 → 그 원문을 seed로 후속 보도 검색."""
    modes = set(strategy.get("modes") or [])
    plans = _plans_for(store, lv2, strategy, p2, planner, definition, today)

    if "official_seed" not in modes:
        return [lambda: _strategy_entries(plans, strategy, lv2, ctx, rerank_llm, rerank_cfg)]

    def expand():
        followups = []
        if official_seeds:
            followups = _plans_for(store, lv2, strategy, p2, planner, definition, today,
                                   extra_seeds=official_seeds, query_kind="event")
            for plan in followups:
                plan.seed_url = official_seeds[0]["source_url"]
        else:
            log.info("%s 공식기관 seed 0건 — 후속 보도 검색어를 새로 만들지 않는다", lv2)
        # seed가 없어도 1라운드에서 쓰지 않은 검색형 계획은 여기서 소진한다.
        return _strategy_entries(plans + followups, strategy, lv2, ctx, rerank_llm, rerank_cfg,
                                 methods={"web_search", "serpapi_site", "board_list"})

    return [
        lambda: _strategy_entries(plans, strategy, lv2, ctx, rerank_llm, rerank_cfg, methods=_OFFICIAL),
        expand,
    ]


def _merged_strategy(lv2: str, p2: dict) -> dict:
    """전략에 기존 intent config의 가점·감점 어휘를 얹는다(acceptance의 LV2 evidence 후보)."""
    strategy = dict((p2.get("source_strategies_by_lv2", {}) or {}).get(lv2) or {})
    if not strategy:
        return {}
    manual = (p2.get("collection_intents_by_lv2", {}) or {}).get(lv2, {})
    for key in ("include_by_type", "include", "exclude", "event_terms"):
        strategy.setdefault(key, manual.get(key, [] if key != "include_by_type" else {}))
    return strategy


def small_run(lv2s: list[str] | None, limit: int, config_path: str, db_path: str,
              taxonomy_config: str = "configs/taxonomy.yaml",
              site_config: str = "configs/site_policy.yaml",
              settings_config: str = "configs/crawler_settings.yaml",
              overrides: dict | None = None, provider=None,
              report_path: str | None = None, on_progress=None,
              discovery_cache: dict[str, list[dict]] | None = None,
              reference_db: str | None = None,
              query_types: dict[str, list[str]] | None = None,
              taxonomy_run_id: str = "") -> dict:
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
    # 실행 이력을 사람이 구분할 수 있도록 로컬 실행 시각과 짧은 충돌 방지 suffix를 함께 저장한다.
    run_id = f"{provider.name}_{_dt.datetime.now().astimezone():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    collected_at = _dt.date.today().isoformat()

    # 목표/커버리지 (deficit 계산용)
    targets = _coverage.resolve_targets(policies, ts)
    initial_cov = _coverage.weighted_coverage_by_lv2(store.conn, 0.0)
    collected: dict = defaultdict(float)
    lv1_by_lv2 = {policy.taxonomy_lv2: policy.taxonomy_lv1 for policy in policies}
    definition_by_lv2 = {policy.taxonomy_lv2: policy.definition for policy in policies}

    # 계획 기반 2차 경로. OpenAI는 여기(검색 계획)에만 쓰고 본문 분류에는 쓰지 않는다.
    today = _dt.date.today()
    planner = _qp.QueryPlanner(rerank_llm, p2.get("query_planner", {}))
    router_ctx = _router.RouterContext(p2=p2, registry=registry, fetcher=extractor.fetcher, today=today)
    accept_policy = p2.get("default_acceptance", {})

    def deficit_of(lv2: str) -> float:
        return targets.get(lv2, 0.0) - (initial_cov.get(lv2, 0.0) + collected[lv2])

    intents = preview_intents(config_path, db_path, taxonomy_config, overrides)
    if lv2s:
        intents = [it for it in intents if it["lv2"] in set(lv2s)]
    if query_types:
        filtered = []
        for item in intents:
            wanted_types = set(query_types.get(item["lv2"], []))
            if not wanted_types:
                filtered.append(item)
                continue
            intent = item["intent"]
            queries = [query for query in intent.queries if intent.query_types.get(query) in wanted_types]
            if queries:
                item = {**item, "intent": replace(
                    intent, queries=queries,
                    query_types={query: intent.query_types[query] for query in queries},
                    max_searches=len(queries),
                )}
                filtered.append(item)
        intents = filtered
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
    qmeta: dict = {}   # 계획 경로의 query별 provenance(source/plan). 다음 라운드 튜닝 근거.
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
        strategy = _merged_strategy(target_lv2, p2)
        # 전략 LV2의 종료 조건은 fetch 수가 아니라 저장량이다(Type=검색 예산, LV2=저장 목표).
        store_target = float(strategy.get("lv2_store_target", 0)) if strategy else 0.0
        official_seeds: list[dict] = []
        if discovery_cache is not None and target_lv2 in discovery_cache:
            rounds = [lambda cached=discovery_cache[target_lv2]: cached]
        elif strategy:
            rounds = _strategy_rounds(
                store, target_lv2, strategy, p2, planner, definition_by_lv2.get(target_lv2, ""),
                today, router_ctx, rerank_llm, rerank_cfg, official_seeds)
        else:
            rounds = [lambda: preview_discovery(intent, provider, rerank_llm, rerank_cfg)]
        for entry in _staged_entries(rounds):
            if total_fetch >= max_total_fetch:
                break
            if store_target and collected[target_lv2] >= store_target:
                log.info("%s 저장 목표 %d건 도달 — 다음 LV2로", target_lv2, int(store_target))
                break
            result, rr = entry["result"], entry["rerank"]
            cand = to_candidate(result, registry)
            cand.run_id = run_id
            cand.taxonomy_run_id = taxonomy_run_id
            cand.query_id = _query_id(result.query_or_intent)
            # SerpAPI의 site: 확장 query도 원래 type을 유지한다.
            cand.subtype_candidate = getattr(result, "query_type", "") or intent.query_types.get(result.query_or_intent, "")
            plan, source = entry.get("plan"), entry.get("source")
            if source:
                cand.source_id, cand.source_access = source["id"], source.get("access", "direct")
            if plan:
                cand.target_type = cand.subtype_candidate = plan.target_type
                cand.query_plan_id = plan.plan_id
                cand.query_generation_source = plan.generation_source
                cand.expected_korea_evidence = plan.expected_korea_evidence
                cand.expected_lv2_evidence = plan.expected_lv2_evidence
                store.bump_plan_stat(plan.plan_id, "discovered_count")
            cand.discovery_relevance_score = rr.discovery_relevance_score
            cand.korea_relevance_score = rr.korea_relevance_score
            prov = cand.discovery_provider or provider.name
            st = stats[prov]
            qs = qstats[result.query_or_intent]
            qlv2.setdefault(result.query_or_intent, target_lv2)
            if plan:
                qmeta.setdefault(result.query_or_intent, {
                    "source_id": cand.source_id, "query_plan_id": plan.plan_id,
                    "generation_source": plan.generation_source})
            st["candidate_count"] += 1
            qs["candidate_count"] += 1
            cand.status = "discovered"

            if rr.fetch_decision == "skip":
                cand.filter_reason = f"rerank_skip:{rr.reason}"[:200]
                cand.status = "rerank_skipped"
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "rerank", "skip", cand.filter_reason, target_lv2, "")
                continue
            # direct가 아닌 source는 발견 메타까지만 쓴다. 본문 fetch도, snippet 저장도 하지 않는다.
            if cand.source_access != "direct":
                cand.status, cand.filter_reason = "discovery_only", f"source_access:{cand.source_access}"
                store.save_candidate(cand)
                continue
            key = cand.dedup_key()
            subtype = cand.subtype_candidate or "lv2_common"
            # 전략 LV2는 Type을 검색 호출 예산(query_budget)으로만 제한한다. fetch 상한은 두지 않는다.
            type_over = not strategy and fetched_type[subtype] >= type_cap
            if key in seen or fetched_lv2 >= max_fetch_lv2 or type_over:
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
            store.bump_plan_stat(cand.query_plan_id, "fetch_success_count")
            rec = outcome.record
            rec.run_id, rec.taxonomy_run_id = run_id, taxonomy_run_id
            rec.collection_phase, rec.query_id = 2, cand.query_id
            rec.discovery_provider, rec.discovery_query = prov, result.query_or_intent
            rec.discovery_relevance_score = rr.discovery_relevance_score
            rec.korea_relevance_score = rr.korea_relevance_score
            rec.taxonomy_lv2_candidate = target_lv2
            rec.subtype_candidate = cand.subtype_candidate
            clean_record(rec, _TREND_PRESERVATION)
            artifact.save_record(rec)

            q = quality.check(rec)
            if q.status == "fail":
                cand.status, cand.filter_reason = "quality_failed", q.reason
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "quality", "fail", q.reason, target_lv2, "")
                continue

            # [12] 전략 LV2: OpenAI 본문 분류 대신 결정론적 acceptance gate가 저장을 판정한다.
            # 여기를 통과한 레코드의 taxonomy_lv2는 예측값이 아니라 "목표 LV2가 확정된 값"이다.
            if strategy and not verify_openai:
                decision = _acceptance.evaluate(rec, cand, strategy, accept_policy, today)
                if decision.accepted:
                    store.bump_plan_stat(cand.query_plan_id, "domestic_pass_count")
                    store.bump_plan_stat(cand.query_plan_id, "lv2_pass_count")
                else:
                    cand.status, cand.filter_reason = "trend_discard", decision.reason
                    store.save_candidate(cand)
                    store.log_filter(cand.source_url, "phase2_acceptance", "fail",
                                     decision.reason, target_lv2, cand.target_type)
                    st["discard"] += 1
                    qs["discard"] += 1
                    continue
                rec.taxonomy_lv1 = lv1_by_lv2.get(target_lv2)
                rec.taxonomy_lv2 = target_lv2
                rec.target_type = cand.target_type
                rec.category = rec.subtype = cand.target_type or "-"
                rec.source_id, rec.query_plan_id = cand.source_id, cand.query_plan_id
                rec.korea_relevance_type = "domestic_direct"
                rec.korea_evidence, rec.lv2_evidence = decision.korea_evidence, decision.lv2_evidence
                rec.is_official_seed = (source or {}).get("method") == "official_board"
                rec.classification_source = "targeted_acceptance_gate"
                action, reason = "accepted", decision.reason
            # 전략이 없는 LV2는 기존 동작(목표 taxonomy 신뢰) 그대로 둔다.
            elif not verify_openai:
                rec.taxonomy_lv1 = lv1_by_lv2.get(target_lv2)
                rec.taxonomy_lv2 = target_lv2
                rec.category = rec.subtype = cand.subtype_candidate or "-"
                action, reason = "accepted", f"{provider.name}_targeted_accept"
                rec.classification_source = f"{provider.name}_targeted"
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
            if rec.is_official_seed:
                # 공식기관 원문은 최종 콘텐츠이자 후속 보도 검색의 seed다.
                official_seeds.append({"title": rec.title, "published_at": rec.published_at,
                                       "site_name": rec.site_name, "source_url": rec.source_url})
                if (source or {}).get("store_seed") is False:
                    cand.status = "seed_only"
                    store.save_candidate(cand)
                    continue
            _phase2_store(store, cand, rec, save_raw_text, target_lv2)
            store.bump_plan_stat(cand.query_plan_id, "stored_count")
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
    report["provider_usage"] = provider.usage_summary()
    report["tavily_usage"] = report["provider_usage"]  # UI 이전 호환
    report["by_query"] = [
        {"query": q, "lv2": qlv2.get(q, ""), **qmeta.get(q, {}), **s}
        for q, s in sorted(qstats.items(), key=lambda kv: kv[1]["accepted"], reverse=True)
    ]
    # 계획별 누적 성과(실행 간 누적). 검색어 교체·source 우선순위 조정은 사람이 결정한다.
    report["query_plans"] = [
        {k: v for k, v in row.items() if k not in ("expected_korea_evidence", "expected_lv2_evidence")}
        for lv2 in {it["lv2"] for it in intents} if _merged_strategy(lv2, p2)
        for row in store.load_query_plans(lv2)
    ]
    if router_ctx.usage:
        report["provider_usage"] = router_ctx.usage_summary()
    if report_path:
        export_report(report, report_path)
    print_report(report)
    store.close()
    return report


def run_taxonomy_plan(lv2s: list[str], config_path: str, db_path: str,
                      taxonomy_config: str = "configs/taxonomy.yaml",
                      site_config: str = "configs/site_policy.yaml",
                      settings_config: str = "configs/crawler_settings.yaml",
                      max_fetch_per_type: int = 6, verify_openai: bool = False,
                      reference_db: str | None = None) -> dict:
    """LV2 → 부족 type → provider 순서로 기존 small_run을 재사용하는 2차 coordinator."""
    plan_rows = {row["lv2"]: row for row in preview_taxonomy_plan(config_path, db_path, taxonomy_config)}
    reports = []
    taxonomy_run_id = f"taxonomy_{_dt.datetime.now().astimezone():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    p2_cfg = _load_phase2_config(config_path)
    for lv2 in lv2s:
        plan = plan_rows.get(lv2)
        if not plan or plan["shortfall"] <= 0:
            continue
        # 전략 LV2는 source·예산·종료 조건을 전략이 이미 정한다.
        # type×provider로 쪼개 여러 번 돌리면 계획만 반복 생성되고 같은 source를 중복 호출한다.
        if _merged_strategy(lv2, p2_cfg):
            report = small_run(
                [lv2], 0, config_path, db_path, taxonomy_config, site_config, settings_config,
                overrides={"adjudication": {"openai_verification": verify_openai},
                           "limits": {"max_selected_lv2": 1}},
                reference_db=reference_db, taxonomy_run_id=taxonomy_run_id,
            )
            reports.append({"lv2": lv2, "type": "-", "provider": "source_strategy", **report})
            continue
        for type_plan in plan["types"]:
            if type_plan["shortfall"] <= 0:
                continue
            for provider_name in type_plan["provider_order"]:
                provider = _provider_for_name(provider_name, _load_phase2_config(config_path))
                if hasattr(provider, "available") and not provider.available():
                    continue
                report = small_run(
                    [lv2], max_fetch_per_type, config_path, db_path, taxonomy_config, site_config, settings_config,
                    overrides={"adjudication": {"openai_verification": verify_openai},
                               "limits": {"max_selected_lv2": 1, "max_fetch_per_type": max_fetch_per_type,
                                          "max_fetch_per_lv2": max_fetch_per_type, "max_total_fetch": max_fetch_per_type}},
                    provider=provider, reference_db=reference_db,
                    query_types={lv2: [type_plan["type"]]},
                    taxonomy_run_id=taxonomy_run_id,
                )
                reports.append({"lv2": lv2, "type": type_plan["type"], "provider": provider_name, **report})
                refreshed = next(row for row in preview_taxonomy_plan(config_path, db_path, taxonomy_config)
                                 if row["lv2"] == lv2)
                refreshed_type = next(row for row in refreshed["types"] if row["type"] == type_plan["type"])
                if refreshed_type["shortfall"] <= 0:
                    break
            refreshed_lv2 = next(row for row in preview_taxonomy_plan(config_path, db_path, taxonomy_config)
                                 if row["lv2"] == lv2)
            if refreshed_lv2["shortfall"] <= 0:
                break
    return {"mode": "taxonomy_first", "taxonomy_run_id": taxonomy_run_id, "runs": reports,
            "plan": preview_taxonomy_plan(config_path, db_path, taxonomy_config)}


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
    # taxonomy 맞춤 수집은 검수 off일 때도 바로 통합한다. 이후 재검수를 요청하면
    # 기존 미검수 candidate와 targeted accepted를 모두 같은 방식으로 다시 판정한다.
    where = [
        "run_id=?", "collection_phase=2",
        "(classification_source LIKE '%_unverified' OR classification_source LIKE '%_targeted')",
    ]
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
            WHERE run_id=? AND source_url=? AND status IN ('trend_candidate','trend_accepted')
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
