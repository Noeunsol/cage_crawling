"""gap_filling(2차 semantic) 모드 — 부족 taxonomy를 Tavily 발견으로 보강."""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import math
import random
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import replace
from urllib.parse import urlparse

import yaml

from src.phase2 import coverage as _coverage
from src.phase2.config import apply_overrides, load_phase2_config
from src.common.clean import clean_record
from src.common.storage.dedup import EventDeduper, make_event_key, simhash
from src.common.extract import ExtractorRouter
from src.phase2.intent_builder import (
    build_collection_intent,
    missing_manual_intents,
    validate_collection_intent,
)
from src.phase2 import acceptance as _acceptance
from src.phase2 import query_planner as _qp
from src.phase2 import source_router as _router
from src.phase2.provider import MockTavilyProvider, SerpApiProvider, TavilyProvider, to_candidate
from src.phase2.reranker import _term_hits, rerank
from src.common.policy import load_policies
from src.common.filtering.quality import QualityFilter
from src.common.schema import canonicalize_url
from src.common.site_registry import SiteRegistry
from src.common.storage.store import Store
from src.common.classify import build_llm as _build_llm
from src.common.report import print_report
from src.phase2.review import build_run_report

log = logging.getLogger(__name__)

def _query_id(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


# 국내 언론의 외국어판. 같은 도메인이라 site: 검색에 딸려 오는데 본문이 한국어가 아니라
# 항상 quality의 not_korean으로 버려진다 — fetch 예산만 먹으므로 URL에서 미리 끊는다.
# (예: donga.com/en/article/... , en.yna.co.kr , english.hani.co.kr)
_FOREIGN_EDITION_SEGMENTS = {"en", "english", "ja", "jp", "zh", "cn", "chinese", "japanese"}


def _non_article_url_reason(url: str) -> str:
    """fetch할 가치가 없는 URL을 본문을 가져오기 전에 걸러낸다."""
    parsed = urlparse(url)
    if parsed.path in ("", "/"):
        return "homepage_url"          # 개별 콘텐츠가 아니라 홈/포털 페이지
    host_labels = parsed.netloc.lower().split(".")
    if host_labels and host_labels[0] in _FOREIGN_EDITION_SEGMENTS:
        return f"foreign_edition:{host_labels[0]}"
    segments = [seg for seg in parsed.path.lower().split("/") if seg]
    if segments and segments[0] in _FOREIGN_EDITION_SEGMENTS:
        return f"foreign_edition:{segments[0]}"
    return ""


def _accepted_keys(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """LV2별 누적 accepted 콘텐츠. DB를 합칠 때 canonical URL로 중복 제거한다."""
    out: dict[str, set[str]] = defaultdict(set)
    for lv2, key in conn.execute(
        """SELECT taxonomy_lv2, COALESCE(NULLIF(canonical_url,''), content_id)
           FROM content_records
           WHERE action='accepted' AND COALESCE(is_supplementary,0)=0
             AND taxonomy_lv2 IS NOT NULL AND taxonomy_lv2!=''"""):
        out[lv2].add(key)
    return out


def default_provider(p2: dict | None = None):
    options = ((p2 or {}).get("providers", {}).get("tavily", {}))
    p = TavilyProvider(options=options)
    if p.available():
        return p
    log.warning("Tavily API 키 또는 SDK 없음 → MockTavilyProvider(오프라인/결정론적) 사용")
    return MockTavilyProvider()


def provider_for_name(name: str, p2: dict):
    """taxonomy plan의 provider 이름을 기존 discovery provider로 연결한다."""
    if name == "serpapi":
        serpapi = p2.get("serpapi", {})
        return SerpApiProvider(serpapi.get("provider", {}), serpapi.get("rules_by_lv2", {}))
    return default_provider(p2)


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
    p2 = load_phase2_config(config_path)
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


def _definition_with_types(policy) -> str:
    """LV2 정의 + type별 설명. planner 프롬프트가 이걸 그대로 읽는다."""
    lines = [policy.definition or ""]
    for subtype in policy.subtypes:
        desc = (getattr(subtype, "description", "") or "").strip()
        if desc:
            lines.append(f"  - {subtype.name}: {desc}")
    return "\n".join(filter(None, lines))


def _phase2_llm(settings: dict):
    """rerank/사후 재검수용 LLMMatcher(있으면). matching.llm.enabled=false면 None."""
    return _build_llm(settings)


def preview_intents(config_path: str, db_path: str,
                    taxonomy_config: str = "configs/taxonomy.yaml",
                    overrides: dict | None = None) -> list[dict]:
    """stage1: 부족 LV2 랭킹 + LV2별 collection intent. API/fetch 없음."""
    p2 = apply_overrides(load_phase2_config(config_path), overrides)
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
    provider = provider or default_provider()
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
               today, extra_seeds: list[dict] | None = None, query_kind: str = "",
               current_count: float | None = None) -> list[_qp.QueryPlan]:
    """계획 생성 또는 재사용. 재사용이면 OpenAI를 호출하지 않는다."""
    cfg = p2.get("query_planner", {})
    seeds = _seed_signals(store, lv2, int(cfg.get("max_seed_items", 20))) + list(extra_seeds or [])
    cached = _qp.latest_generation(store.load_query_plans(lv2))
    # 살 검색어 수는 max_queries_per_lv2 하나로 정한다. 이미 모은 양은 보지 않는다 —
    # 목표는 부족분 랭킹용이고 수집을 막는 장치가 아니다.
    max_total = _qp.max_queries(cfg)
    # YAML 검색어 모드(planner enabled=false)는 캐시를 타지 않는다. 캐시에는 예전 OpenAI 계획이
    # 들어 있어서, 그대로 재사용하면 "YAML 쿼리 사용"을 골라도 OpenAI 검색어가 나간다.
    use_yaml_queries = not cfg.get("enabled", True)
    if not use_yaml_queries and not (query_kind or _qp.should_regenerate(cached, seeds, cfg, today)):
        # 재사용은 저장하지 않는다. 다시 쓰면 created_at이 갱신돼 max_age_days가 영원히 안 온다.
        # 캐시도 목표만큼만 쓴다. 그대로 다 쓰면 UI에서 목표를 낮춰도 크레딧이 그대로다
        # (실측 2026-08-20: 목표 100→20으로 내려도 캐시된 22개가 다 나갔다).
        return _qp.plans_from_rows(cached)[:max_total]
    # YAML 모드는 planner를 아예 부르지 않는다(호출해도 enabled=false라 빈 리스트다).
    plans = [] if use_yaml_queries else planner.plan(
        lv2, definition, strategy, seeds,
        _qp.low_performing_queries(cached), query_kind, max_queries=max_total)
    if not plans:
        plans = yaml_query_plans(lv2, strategy, p2, max_total)
        if len(plans) < max_total:
            log.warning("%s config fallback 검색어 %d개 — Gap 기준 필요 호출 %d회보다 적어 목표 미달 가능",
                        lv2, len(plans), max_total)
    if plans:
        store.save_query_plans(
            [p.to_row(_qp.seed_fingerprint(seeds), today.isoformat()) for p in plans])
    return plans


def yaml_query_plans(lv2: str, strategy: dict, p2: dict, max_total: int) -> list[_qp.QueryPlan]:
    """collection_intents_by_lv2의 손으로 쓴 검색어를 계획으로 만든다. API·DB를 쓰지 않는다.

    shuffle_fallback_plans면 **예산으로 자르기 전에** 섞는다. 자른 뒤 섞으면 순서만 바뀌고
    쓰이는 검색어는 매번 같아서 "랜덤"이 아무 의미가 없다.
    shuffle_seed를 주면 결과가 고정된다 — UI 미리보기와 실제 실행을 일치시키는 데 쓴다.
    """
    cfg = p2.get("query_planner", {})
    manual = (p2.get("collection_intents_by_lv2", {}) or {}).get(lv2, {})
    serpapi_rule = (p2.get("serpapi", {}).get("rules_by_lv2", {}) or {}).get(lv2, {})
    plans = _qp.fallback_plans(lv2, strategy, manual.get("queries_by_type", {}),
                               manual.get("include_by_type", {}),
                               serpapi_rule.get("domains_by_type"))
    if cfg.get("shuffle_fallback_plans"):
        random.Random(cfg.get("shuffle_seed")).shuffle(plans)
    # 채널 배분 → 예산 절단 순서를 지킨다. 순서를 뒤집으면 잘린 뒤 비율이 무너진다.
    return _qp.apply_budgets(_qp.balance_sources(plans, strategy), strategy, max_total)


# source의 method가 곧 어느 검색 API로 나가는지를 정한다(source_router._METHODS와 같은 매핑).
_PROVIDER_BY_METHOD = {
    "web_search": "Tavily",
    "serpapi_site": "SerpAPI",
    "official_board": "SerpAPI",
    "board_list": "게시판 목록",
}


def provider_of(method: str) -> str:
    return _PROVIDER_BY_METHOD.get(method, method or "—")


def preview_yaml_queries(config_path: str, lv2s: list[str], overrides: dict | None = None) -> dict:
    """실행 전에 'YAML 검색어 모드'가 쓸 검색어를 provider까지 붙여 그대로 보여준다.

    비용 0(설정만 읽는다). 검색어를 쓰지 않는 board_list source도 함께 돌려준다 —
    검색어 표에만 의존하면 "게시판은 안 도는 줄" 오해하게 된다.
    """
    p2 = apply_overrides(load_phase2_config(config_path), overrides)
    max_total = _qp.max_queries(p2.get("query_planner", {}))
    out: dict[str, dict] = {}
    for lv2 in lv2s:
        strategy = _merged_strategy(lv2, p2)
        if not strategy:
            continue
        by_id = {s["id"]: s for s in strategy.get("sources") or []}
        today = _dt.date.today()
        queries = []
        for plan in yaml_query_plans(lv2, strategy, p2, max_total):
            source = by_id.get(plan.source_id) or {}
            queries.append({"query": plan.query, "target_type": plan.target_type,
                            "source_id": plan.source_id,
                            "provider": provider_of(source.get("method", "")),
                            # 실제로 provider에 나가는 검색식(SerpAPI는 site: 확장이 붙는다)
                            "final_query": _router.final_query(plan, source, p2, lv2),
                            "targets": source.get("domains")
                                       or ([source["domain"]] if source.get("domain") else [])})
        # source별 실제 요청 옵션. 실행과 같은 함수로 만든다(미리보기가 어긋나지 않게).
        execution = []
        for src in strategy.get("sources") or []:
            method = src.get("method", "")
            if method == "web_search":
                opt = _router.tavily_options(src, strategy, p2, today)
                execution.append({"source_id": src["id"], "provider": "Tavily", "options": {
                    "검색 깊이": opt.get("search_depth"),
                    "색인": opt.get("topic", "general"),
                    "쿼리당 결과": opt.get("max_results_per_query"),
                    "발행일 하한": opt.get("start_date") or f"time_range={opt.get('time_range')}",
                    "국가 가중치": opt.get("country"),
                    "도메인 화이트리스트": len(opt.get("include_domains") or []) or "없음",
                    "제외 도메인": len(opt.get("excluded_domains") or []),
                }})
            elif method in ("serpapi_site", "official_board"):
                rule = _router.serpapi_rule(src, strategy, p2, lv2)
                prov = p2.get("serpapi", {}).get("provider", {})
                domains = src.get("domains") or ([src["domain"]] if src.get("domain") else [])
                execution.append({"source_id": src["id"], "provider": "SerpAPI", "options": {
                    "엔진": f"{prov.get('engine')} · {prov.get('google_domain')}",
                    "지역·언어": f"gl={prov.get('gl')} hl={prov.get('hl')}",
                    "쿼리당 결과": prov.get("num"),
                    "기간 필터": rule.get("tbs") or "제한 없음",
                    "site: 방식": (f"{len(domains)}개 도메인을 OR 한 줄로 (1크레딧)"
                                   if len(domains) > 1 else f"site:{domains[0]}" if domains else "—"),
                }})
            elif method == "board_list":
                execution.append({"source_id": src["id"], "provider": "게시판 목록", "options": {
                    "방식": "검색어 없이 최신 목록 페이지를 직접 읽음 (크레딧 0)",
                    "페이지": src.get("max_pages", 1),
                    "요청 간격": f"{src.get('request_delay', 2.0)}초",
                }})
        boards = [
            {"source_id": s["id"], "provider": provider_of(s["method"]),
             "targets": [g.get("name") or g.get("id") for g in (s.get("galleries") or [])]}
            for s in strategy.get("sources") or []
            if s.get("method") == "board_list" and s.get("access") != "blocked"
        ]
        # 검색어가 한 건도 안 배정된 source. "설정에는 있는데 이번 실행엔 안 나간다"를 드러낸다.
        used = {q["source_id"] for q in queries} | {b["source_id"] for b in boards}
        idle = [{"source_id": s["id"], "provider": provider_of(s.get("method", ""))}
                for s in strategy.get("sources") or []
                if s["id"] not in used and s.get("access") != "blocked"]
        # official_seed(2-hop) 전략은 1라운드에서 공식기관 메타만 읽고, 2라운드에서
        # 그 seed로 만든 검색어가 뉴스·웹으로 나간다. 아래 미리보기는 1라운드분이다.
        out[lv2] = {"queries": queries, "boards": boards, "idle": idle,
                    "execution": execution,
                    "two_hop": "official_seed" in set(strategy.get("modes") or [])}
    return out


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


def _seed_text(cand, result=None) -> str:
    return " ".join(filter(None, [cand.title or "", cand.snippet or "", cand.content_hint or ""]))


def _is_incident_seed(cand, strategy: dict) -> bool:
    """공식기관 SERP는 '목 차'·'알림마당' 같은 목록 페이지가 태반이다.

    사건어나 물질어가 제목에 하나도 없으면 검색어 생성에 쓸모가 없으므로 seed에서 뺀다.
    """
    text = _seed_text(cand)
    terms = list(strategy.get("event_terms") or []) + [
        term for values in (strategy.get("include_by_type") or {}).values() for term in values]
    return bool(_term_hits(text, terms))



def _source_strategy(strategy: dict, source: dict | None) -> dict:
    """소스별 override를 얹은 전략. 한 LV2 안에 성격이 다른 소스를 함께 둘 때 쓴다.

    1_A가 대표 사례다. 언론 기사는 '한국 사건 + 온라인 맥락 + 사건 어휘'를 요구해야 하지만,
    디시 원문은 그 조건을 하나도 못 채운다(제목이 '숲음갤 병신인것도 맞는데'다).
    기준을 통째로 풀면 뉴스 쪽에 잡문이 들어오므로 소스 단위로만 완화한다.
    """
    overrides = (source or {}).get("overrides")
    if not overrides:
        return strategy
    merged = {**strategy, **overrides}
    if "acceptance" in overrides:      # acceptance만 한 겹 더 병합한다
        merged["acceptance"] = {**(strategy.get("acceptance") or {}), **overrides["acceptance"]}
    return merged



def _staged_entries(rounds):
    """라운드를 지연 평가한다. 2라운드 계획은 1라운드 저장이 끝난 뒤에 만들어져야 한다."""
    for make_entries in rounds:
        yield from _relevance_first_entries(make_entries())


_OFFICIAL = {"official_board"}


def _strategy_rounds(store, lv2, strategy, p2, planner, definition, today, ctx,
                     rerank_llm, rerank_cfg, official_seeds: list[dict], approved=None,
                     current_count: float | None = None):
    """official_seed 모드는 2-hop: 공식기관 원문 수집 → 그 원문을 seed로 후속 보도 검색.

    approved가 오면(=UI에서 사람이 검토·선택한 계획) 새로 만들지 않고 그대로 실행한다.
    """
    modes = set(strategy.get("modes") or [])
    if approved is not None:
        plans = approved[:_qp.max_queries(p2.get("query_planner", {}))]
    else:
        plans = _plans_for(store, lv2, strategy, p2, planner, definition, today,
                           current_count=current_count)

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


def preview_query_plans(config_path: str, db_path: str, lv2s: list[str] | None = None,
                        settings_config: str = "configs/crawler_settings.yaml",
                        taxonomy_config: str = "configs/taxonomy.yaml",
                        overrides: dict | None = None) -> dict[str, list]:
    """전략 LV2의 검색 계획만 만들어 돌려준다. 검색·fetch·저장 없음(OpenAI 호출만).

    UI가 사람에게 보여주고 선택받은 뒤 small_run(plan_cache=...)로 그대로 실행한다.
    """
    with open(settings_config, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    p2 = apply_overrides(load_phase2_config(config_path), overrides)
    policies = load_policies(taxonomy_config)
    # LV2 정의만 넘기면 planner가 type 이름 다섯 글자로 검색어를 지어낸다.
    # taxonomy.yaml에 type별 설명이 이미 있으므로 함께 넘겨 구체적인 행위를 노리게 한다.
    definition_by_lv2 = {policy.taxonomy_lv2: _definition_with_types(policy)
                         for policy in policies}
    planner = _qp.QueryPlanner(_phase2_llm(settings), p2.get("query_planner", {}))
    store = Store(db_path)
    today = _dt.date.today()
    out: dict[str, list] = {}
    for lv2 in (lv2s or list(p2.get("source_strategies_by_lv2", {}) or {})):
        strategy = _merged_strategy(lv2, p2)
        if not strategy:
            continue
        out[lv2] = _plans_for(store, lv2, strategy, p2, planner,
                              definition_by_lv2.get(lv2, ""), today)
    store.close()
    return out


def query_plan_limits(config_path: str, db_path: str, lv2s: list[str],
                      overrides: dict | None = None, reference_db: str | None = None) -> dict[str, int]:
    """현재 DB Gap을 반영한 LV2별 실제 유료 검색 호출 상한."""
    p2 = apply_overrides(load_phase2_config(config_path), overrides)
    targets = _coverage.resolve_targets(load_policies("configs/taxonomy.yaml"),
                                        p2.get("target_selection", {}))
    per_query = float((p2.get("query_planner", {}) or {}).get("expected_stored_per_query", 5))
    cap = _qp.max_queries(p2.get("query_planner", {}))
    keys: dict[str, set[str]] = defaultdict(set)
    for path in dict.fromkeys([db_path, reference_db]):
        if not path:
            continue
        db = Store(path)
        for lv2, values in _accepted_keys(db.conn).items():
            keys[lv2].update(values)
        db.close()
    return {
        lv2: min(cap, max(0, math.ceil((targets.get(lv2, 0.0) - len(keys.get(lv2, set()))) / per_query)))
        for lv2 in lv2s if _merged_strategy(lv2, p2)
    }


def _merged_strategy(lv2: str, p2: dict) -> dict:
    """전략에 기존 intent config의 어휘와 공통 차단 도메인을 얹는다."""
    strategy = dict((p2.get("source_strategies_by_lv2", {}) or {}).get(lv2) or {})
    if not strategy:
        return {}
    manual = (p2.get("collection_intents_by_lv2", {}) or {}).get(lv2, {})
    for key in ("include_by_type", "include", "exclude", "event_terms"):
        strategy.setdefault(key, manual.get(key, [] if key != "include_by_type" else {}))
    global_excluded = (p2.get("providers", {}).get("tavily", {}).get("excluded_domains") or [])
    strategy["excluded_domains"] = list(dict.fromkeys(
        [*global_excluded, *(strategy.get("excluded_domains") or [])]
    ))
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
              taxonomy_run_id: str = "",
              plan_cache: dict | None = None) -> dict:
    """stage3: 선택 LV2들에 discovery→rerank→fetch/extract→clean→classify→판정→dedup→저장."""
    with open(settings_config, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    p2 = apply_overrides(load_phase2_config(config_path), overrides)
    policies = load_policies(taxonomy_config)
    ts = p2.get("target_selection", {})
    limits = p2.get("limits", {})
    max_lv2 = int(limits.get("max_selected_lv2", 5))
    max_fetch_lv2 = int(limits.get("max_fetch_per_lv2", 20))
    max_fetch_type = int(limits.get("max_fetch_per_type", max_fetch_lv2))
    max_total_fetch = min(int(limits.get("max_total_fetch", 60)), limit) if limit else int(limits.get("max_total_fetch", 60))
    rerank_cfg = p2.get("rerank", {})
    adjudication = p2.get("adjudication", {})
    permissive = bool(adjudication.get("permissive_collection", False))

    registry = SiteRegistry.load(site_config)
    extractor = ExtractorRouter(registry, settings)
    quality = QualityFilter(settings)
    # rerank LLM은 fetch 전 애매밴드에서만 1회 호출한다. 수집 중 본문 재분류는 하지 않는다 —
    # 저장 판정은 acceptance gate(전략 LV2) 또는 목표 taxonomy 신뢰(그 외)가 담당하고,
    # LLM 본문 검수는 수집이 끝난 뒤 review.verify_unverified_candidates로만 돈다.
    rerank_llm = _phase2_llm(settings)
    dedup_threshold = settings.get("dedup", {}).get("event_hamming_threshold", 3)
    deduper = EventDeduper(dedup_threshold)
    save_raw_text = settings.get("privacy", {}).get("save_raw_text", True)
    store = Store(db_path)
    provider = provider or default_provider(p2)
    # 실행 이력을 사람이 구분할 수 있도록 로컬 실행 시각과 짧은 충돌 방지 suffix를 함께 저장한다.
    run_id = f"{provider.name}_{_dt.datetime.now().astimezone():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    collected_at = _dt.date.today().isoformat()

    # 목표/커버리지 (deficit 계산용)
    targets = _coverage.resolve_targets(policies, ts)
    initial_cov = _coverage.weighted_coverage_by_lv2(store.conn, 0.0)
    collected: dict = defaultdict(float)
    lv1_by_lv2 = {policy.taxonomy_lv2: policy.taxonomy_lv1 for policy in policies}
    # LV2 정의만 넘기면 planner가 type 이름 다섯 글자로 검색어를 지어낸다.
    # taxonomy.yaml에 type별 설명이 이미 있으므로 함께 넘겨 구체적인 행위를 노리게 한다.
    definition_by_lv2 = {policy.taxonomy_lv2: _definition_with_types(policy)
                         for policy in policies}

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
    accepted_keys = _accepted_keys(store.conn)
    if reference_db and reference_db != db_path:
        ref = Store(reference_db)
        seen |= {canonicalize_url(u) for u in ref.existing_canonical_urls()}
        for lv2, values in _accepted_keys(ref.conn).items():
            accepted_keys[lv2].update(values)
        ref.close()
        log.info("중복 방지 참조 DB %s → 기준 URL %d건", reference_db, len(seen))
    stats: dict = defaultdict(_prov_stats)
    qstats: dict = defaultdict(_query_stats)  # 쿼리별 성과. 다음 라운드 쿼리 교체의 근거.
    qlv2: dict = {}
    qmeta: dict = {}   # 계획 경로의 query별 provenance(source/plan). 다음 라운드 튜닝 근거.
    # 후보를 한 건도 못 만든 LV2와 그 이유. "완료인데 저장 0건"의 원인을 UI가 읽는다.
    skipped: dict[str, dict] = {}
    total_fetch = 0

    for it in intents:
        intent = it["intent"]
        target_lv2 = it["lv2"]
        lv2_candidates = 0
        fetched_lv2 = 0
        fetched_type: dict[str, int] = defaultdict(int)
        # 단일 type LV2(2_E·4_I·6_Q·6_R·6_S)는 per-type 상한이 곧 LV2 상한이 되어 수집량만 깎인다.
        # LV2 공통 쿼리의 빈 라벨은 type이 아니므로 세지 않는다.
        real_types = {t for t in intent.query_types.values() if t}
        type_cap = max_fetch_type if len(real_types) > 1 else max_fetch_lv2
        strategy = _merged_strategy(target_lv2, p2)
        strategy_permissive = bool(strategy.get("permissive_collection", permissive))
        # 수집을 멈추는 것은 총 fetch 예산과 후보 소진, 둘뿐이다. "목표를 채웠으니 그만"은
        # 두지 않는다 — 목표(target_selection)는 부족분 랭킹용이지 상한이 아니다.
        # LV2별 상한은 배분 장치일 뿐이고 0이면 총예산까지 계속 모은다.
        max_fetch_lv2 = int(strategy.get("max_fetch_per_lv2")
                            or limits.get("max_fetch_per_lv2") or 0)
        # 총 fetch 예산이 이미 바닥이면 여기서 끝낸다. 아래 discovery는 검색 API를 실제로
        # 호출하는데, 예산이 없으면 그 결과로 본문을 한 건도 못 가져온다 — 검색비만 내고 버린다.
        if total_fetch >= max_total_fetch:
            log.warning("총 fetch 예산 %d건 소진 — %s 이후 LV2는 검색조차 하지 않는다(검색비 낭비 방지)",
                        max_total_fetch, target_lv2)
            break
        official_seeds: list[dict] = []
        if discovery_cache is not None and target_lv2 in discovery_cache:
            rounds = [lambda cached=discovery_cache[target_lv2]: cached]
        elif strategy:
            rounds = _strategy_rounds(
                store, target_lv2, strategy, p2, planner, definition_by_lv2.get(target_lv2, ""),
                today, router_ctx, rerank_llm, rerank_cfg, official_seeds,
                approved=(plan_cache or {}).get(target_lv2),
                current_count=len(accepted_keys.get(target_lv2, set())))
        else:
            rounds = [lambda: preview_discovery(intent, provider, rerank_llm, rerank_cfg)]
        for entry in _staged_entries(rounds):
            if total_fetch >= max_total_fetch:
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
            lv2_candidates += 1
            cand.status = "discovered"

            non_article = _non_article_url_reason(cand.source_url)
            if non_article:
                cand.status, cand.filter_reason = "url_filtered", non_article
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "url_filter", "skip", non_article, target_lv2, "")
                continue

            # direct가 아닌 source는 발견 메타까지만 쓴다. 본문 fetch도, snippet 저장도 하지 않는다.
            # rerank보다 먼저 본다 — fetch하지 않을 후보를 관련성 점수로 거를 이유가 없고,
            # 공식기관 SERP는 제목이 빈약해 rerank가 seed를 통째로 날려버린다.
            if cand.source_access != "direct":
                cand.status, cand.filter_reason = "discovery_only", f"source_access:{cand.source_access}"
                store.save_candidate(cand)
                if _is_incident_seed(cand, strategy):
                    official_seeds.append(
                        {"title": cand.title or "", "published_at": cand.published_at_hint,
                         "site_name": cand.site_name, "source_url": cand.source_url})
                continue

            # fetch 여부는 rerank 점수가 정한다. permissive_collection이면 그마저 끄고 다 가져온다
            # (현재 19개 LV2 전부 permissive — 스니펫으로 미리 거르지 말고 본문을 보고 판정한다).
            src_strategy = _source_strategy(strategy, source)
            src_permissive = bool(src_strategy.get("permissive_collection", strategy_permissive))
            if not src_permissive and rr.fetch_decision == "skip":
                cand.filter_reason = f"rerank_skip:{rr.reason}"[:200]
                cand.status = "rerank_skipped"
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "rerank", "skip", cand.filter_reason, target_lv2, "")
                continue
            key = cand.dedup_key()
            subtype = cand.subtype_candidate or "lv2_common"
            # 전략 LV2는 Type을 검색 호출 예산(query_budget)으로만 제한한다. fetch 상한은 두지 않는다.
            type_over = not strategy and fetched_type[subtype] >= type_cap
            # 중복과 예산 초과를 한 사유로 뭉치면 리포트에서 원인을 못 가린다.
            # 실측(2026-08-19 1_A): 136건이 'seen_or_budget_cap'이었는데 진짜 중복은 3건뿐이고
            # 133건이 max_fetch_per_lv2(20)에 막힌 것이었다.
            if key in seen:
                cand.status, cand.filter_reason = "duplicate_url", "already_collected"
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "dedup", "skip", "already_collected", target_lv2, "")
                continue
            lv2_over = bool(max_fetch_lv2) and fetched_lv2 >= max_fetch_lv2
            if lv2_over or type_over:
                cand.status = "budget_exceeded"
                cand.filter_reason = (f"max_fetch_per_lv2={max_fetch_lv2}" if lv2_over
                                      else f"max_fetch_per_type={type_cap}")
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "budget", "skip", cand.filter_reason, target_lv2, "")
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
            clean_record(rec)

            if not (rec.core_text or rec.body_text or "").strip():
                cand.status, cand.filter_reason = "extraction_failed", "empty_body"
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "extract", "fail", "empty_body", target_lv2, "")
                continue

            q = quality.check(rec)
            if q.status == "fail" and (q.reason or "").startswith("not_korean"):
                cand.status, cand.filter_reason = "quality_failed", q.reason
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "quality", "fail", q.reason, target_lv2, "")
                continue
            if q.status == "fail" and not strategy_permissive:
                cand.status, cand.filter_reason = "quality_failed", q.reason
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "quality", "fail", q.reason, target_lv2, "")
                continue

            # [12] 전략 LV2: OpenAI 본문 분류 대신 결정론적 acceptance gate가 저장을 판정한다.
            # 여기를 통과한 레코드의 taxonomy_lv2는 예측값이 아니라 "목표 LV2가 확정된 값"이다.
            if strategy:
                decision = _acceptance.evaluate(rec, cand, src_strategy, accept_policy, today, registry)
                if decision.accepted:
                    store.bump_plan_stat(cand.query_plan_id, "domestic_pass_count")
                    store.bump_plan_stat(cand.query_plan_id, "lv2_pass_count")
                # HARD_REJECTS는 permissive로도 뚫리지 않는다. 수집 기간(stale)은 범위 정의고,
                # 한국 근거 0(no_korea_context)은 최소 자격이라 '품질 완화'와 성격이 다르다.
                elif not strategy_permissive or decision.reason.startswith(_acceptance.HARD_REJECTS):
                    cand.status, cand.filter_reason = "discard", decision.reason
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
                # 게이트를 통과했다고 한국 관련이 확인된 건 아니다(require_domestic_direct를 끌 수 있다).
                rec.korea_relevance_type = "domestic_direct" if decision.domestic else ""
                rec.korea_evidence, rec.lv2_evidence = decision.korea_evidence, decision.lv2_evidence
                rec.is_official_seed = (source or {}).get("method") == "official_board"
                rec.classification_source = (
                    "targeted_acceptance_gate" if decision.accepted else "targeted_discovery"
                )
                action = "accepted"
                reason = decision.reason if decision.accepted else f"permissive_collection:{decision.reason}"
            # 전략이 없는 LV2는 목표 taxonomy를 신뢰해 저장한다.
            else:
                rec.taxonomy_lv1 = lv1_by_lv2.get(target_lv2)
                rec.taxonomy_lv2 = target_lv2
                rec.category = rec.subtype = cand.subtype_candidate or "-"
                action, reason = "accepted", f"{provider.name}_targeted_accept"
                rec.classification_source = f"{provider.name}_targeted"

            rec.action = action
            rec.filter_status = "pass" if action == "accepted" else "review" if action == "candidate" else "fail"
            rec.classification_reason = rec.filter_reason = reason

            if action == "discard":
                cand.status, cand.filter_reason = "discard", reason
                store.save_candidate(cand)
                store.log_filter(cand.source_url, "phase2_classify", "fail", reason,
                                 rec.taxonomy_lv2 or "", rec.category or "")
                st["discard"] += 1
                qs["discard"] += 1
                continue

            # [15] near-dup
            rec.simhash = str(simhash(rec.core_text or rec.body_text))
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
            _store_record(store, cand, rec, save_raw_text, target_lv2)
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

        # 후보가 0건이면 "왜 아무 일도 없었는지"를 남긴다. 화면에 "완료"만 뜨면 오류로 읽힌다.
        if lv2_candidates == 0:
            skipped[target_lv2] = {
                "reason": "no_candidates",
                "already": len(accepted_keys.get(target_lv2, set())),
                "detail": ("검색 결과가 0건입니다. API 키·검색 기간(recency_days)·source 설정을 "
                           "확인하세요."),
            }
            log.warning("%s 후보 0건 — %s", target_lv2, skipped[target_lv2]["detail"])

    # 수집이 끝나면 점검은 review.py 한 곳에서 조립한다.
    report = build_run_report(
        store, run_id=run_id, targets=targets, initial_cov=initial_cov, collected=collected,
        prov_stats=stats, query_stats=qstats, qlv2=qlv2, qmeta=qmeta,
        strategy_lv2s={it["lv2"] for it in intents if _merged_strategy(it["lv2"], p2)},
        provider_usage=router_ctx.usage_summary() if router_ctx.usage else provider.usage_summary(),
        skipped=skipped, report_path=report_path,
    )
    store.close()
    return report


def _store_record(store, cand, rec, save_raw_text, target_lv2) -> None:
    """2차 저장 마무리. 1차(phase1.verdict.finalize)와 로그 stage·상태가 다르다."""
    rec.canonical_url = rec.canonical_url or cand.canonical_url or cand.source_url
    if not save_raw_text:
        rec.raw_text = ""
    store.save_content(rec)
    cand.status = rec.action
    cand.filter_reason = rec.filter_reason
    store.save_candidate(cand)
    store.log_filter(cand.source_url, "phase2_classify", rec.filter_status,
                     rec.filter_reason or rec.action, rec.taxonomy_lv2 or target_lv2, rec.category or "")


def run_taxonomy_plan(lv2s: list[str], config_path: str, db_path: str,
                      taxonomy_config: str = "configs/taxonomy.yaml",
                      site_config: str = "configs/site_policy.yaml",
                      settings_config: str = "configs/crawler_settings.yaml",
                      max_fetch_per_type: int = 6,
                      reference_db: str | None = None, on_progress=None,
                      strategy_by_lv2: dict | None = None,
                      query_plan_mode: str = "openai",
                      max_queries_per_lv2: int | None = None,
                      max_total_fetch: int | None = None,
                      shuffle_seed: int | None = None) -> dict:
    """LV2 → type → provider 순서로 small_run을 재사용하는 2차 coordinator.

    이번 실행의 수집량은 두 손잡이가 정한다.
      max_queries_per_lv2 : 카테고리당 살 검색어 수 상한. **검색 크레딧이 여기서 나간다.**
                            실제 실행은 누적 목표까지 남은 Gap 상한을 한 번 더 적용한다.
      max_total_fetch     : 본문 fetch 상한. HTTP만 쓰므로 크레딧과 무관하고 시간만 든다.
    둘 다 None이면 config 값을 그대로 쓴다.
    query_plan_mode="yaml_random"이면 shuffle_seed로 뽑기 결과를 고정한다 —
    UI가 미리 보여준 검색어와 실제로 나가는 검색어를 같게 만들기 위해서다.

    on_progress(step, total_steps, label, done, total)로 두 층위를 함께 알린다.
    total_steps는 상한 추정치다 — 후보가 떨어지면 실제 실행 수는 더 적을 수 있다.
    """
    # 이번 실행에만 적용할 상한. config를 건드리지 않고 override로만 얹는다.
    run_limits: dict = {}
    if max_total_fetch:
        run_limits["max_total_fetch"] = int(max_total_fetch)
    run_planner: dict = {}
    if max_queries_per_lv2:
        run_planner["max_queries_per_lv2"] = int(max_queries_per_lv2)
    plan_rows = {row["lv2"]: row for row in preview_taxonomy_plan(config_path, db_path, taxonomy_config)}
    reports = []
    taxonomy_run_id = f"taxonomy_{_dt.datetime.now().astimezone():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    p2_cfg = load_phase2_config(config_path)
    gap_query_caps = query_plan_limits(
        config_path, db_path, lv2s, reference_db=reference_db,
        overrides={"query_planner": run_planner} if run_planner else None,
    )

    # 상한 추정: 전략 LV2는 1회, 나머지는 type × provider 순서만큼.
    # 목표(shortfall)로 걸러내지 않는다 — 사람이 고른 카테고리는 그대로 실행한다.
    max_steps = 0
    for lv2 in lv2s:
        row = plan_rows.get(lv2)
        if not row:
            continue
        max_steps += 1 if _merged_strategy(lv2, p2_cfg) else sum(
            len(t["provider_order"]) for t in row["types"])
    step = 0

    def _report(label: str):
        """이 step의 small_run 진행을 UI로 흘려보낸다."""
        if not on_progress:
            return None
        on_progress(step, max_steps, label, 0, 0)
        return lambda done, total: on_progress(step, max_steps, label, done, total)
    for lv2 in lv2s:
        plan = plan_rows.get(lv2)
        if not plan:
            continue
        # 전략 LV2는 source·예산·종료 조건을 전략이 이미 정한다.
        # type×provider로 쪼개 여러 번 돌리면 계획만 반복 생성되고 같은 source를 중복 호출한다.
        if _merged_strategy(lv2, p2_cfg):
            step += 1
            query_planner_override = dict(run_planner)
            gap_cap = gap_query_caps.get(lv2, _qp.max_queries({"max_queries_per_lv2": max_queries_per_lv2 or 0}))
            if gap_cap <= 0:
                reports.append({"lv2": lv2, "type": "-", "provider": "source_strategy",
                                "run_stored": 0, "run_candidates": 0, "runs": [],
                                "skipped": {lv2: {"reason": "target_already_met",
                                                  "detail": "누적 목표를 이미 채워 검색 API를 호출하지 않았습니다."}}})
                continue
            query_planner_override["max_queries_per_lv2"] = gap_cap
            if query_plan_mode == "yaml_random":
                query_planner_override |= {"enabled": False, "shuffle_fallback_plans": True,
                                           "shuffle_seed": shuffle_seed}
            report = small_run(
                [lv2], 0, config_path, db_path, taxonomy_config, site_config, settings_config,
                overrides={"limits": {"max_selected_lv2": 1, **run_limits},
                           "query_planner": query_planner_override,
                           "strategy_by_lv2": strategy_by_lv2 or {}},
                reference_db=reference_db, taxonomy_run_id=taxonomy_run_id,
                on_progress=_report(f"{lv2} · 검색 계획 방식"),
            )
            reports.append({"lv2": lv2, "type": "-", "provider": "source_strategy", **report})
            continue
        for type_plan in plan["types"]:
            for provider_name in type_plan["provider_order"]:
                provider = provider_for_name(provider_name, load_phase2_config(config_path))
                if hasattr(provider, "available") and not provider.available():
                    continue
                step += 1
                report = small_run(
                    [lv2], max_fetch_per_type, config_path, db_path, taxonomy_config, site_config, settings_config,
                    overrides={"limits": {"max_selected_lv2": 1, "max_fetch_per_type": max_fetch_per_type,
                                          "max_fetch_per_lv2": max_fetch_per_type,
                                          "max_total_fetch": max_fetch_per_type},
                               "query_planner": run_planner},
                    provider=provider, reference_db=reference_db,
                    query_types={lv2: [type_plan["type"]]},
                    taxonomy_run_id=taxonomy_run_id,
                    on_progress=_report(f"{lv2} · {type_plan['type']} · {provider_name}"),
                )
                reports.append({"lv2": lv2, "type": type_plan["type"], "provider": provider_name, **report})
    return {"mode": "taxonomy_first", "taxonomy_run_id": taxonomy_run_id, "runs": reports,
            "plan": preview_taxonomy_plan(config_path, db_path, taxonomy_config)}


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
                      load_policies(taxonomy_config), load_phase2_config(config)),
                  "deficits": [
                      {"lv2": it["lv2"], "target": it["target"], "effective": it["effective"],
                       "deficit": it["deficit"], "queries": it["intent"].queries,
                       "warnings": it["warnings"]}
                      for it in intents
                  ]}
        print_report(report)
        return report
    max_total = int(load_phase2_config(config).get("limits", {}).get("max_total_fetch", 60))
    # lv2 선정은 small_run 안의 preview_intents가 그대로 한다. 여기서 미리 부르면 DB/커버리지 계산이 2회.
    return small_run(None, max_total, config, db_path, taxonomy_config, site_config,
                     settings_config, overrides=overrides, report_path=report_path)
