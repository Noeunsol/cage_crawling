"""2차 수집 후 점검 — 수집이 끝난 뒤에 하는 일을 한 곳에 모은다.

네 가지가 여기 있다. 수집 *중*의 저장 게이트는 phase2/acceptance.py로 따로 두었다 —
성격이 다르다(그건 건건이 저장 여부를 정하고, 여기는 이미 저장된 것을 되돌아본다).

  1) build_run_report  : 실행 리포트 조립 (coverage / provider 성과 / 검색어별 / 계획별 누적)
  2) sample_for_review : 수동 표본 검수 CSV. 본문 LLM 검증을 없앤 대가라 생략할 수 없다.
  3) verify_unverified_candidates : 저장된 본문만 OpenAI로 재분류 (검색·fetch 재호출 없음)
  4) adjudicate        : 3)이 쓰는 본문 기준 판정 규칙
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import yaml

from src.common.classify import build_llm, build_taxonomy_index
from src.common.policy import load_policies
from src.common.report import build_report, export_report, print_report
from src.common.schema import ContentRecord
from src.common.storage.store import Store
from src.phase2 import coverage as _coverage
from src.phase2.config import apply_overrides, load_phase2_config


# ── 1) 실행 리포트 ──

def build_run_report(store: Store, *, run_id: str, targets: dict, initial_cov: dict,
                     collected: dict, prov_stats: dict, query_stats: dict,
                     qlv2: dict, qmeta: dict, strategy_lv2s: set[str],
                     provider_usage: dict, skipped: dict | None = None,
                     report_path: str | None = None) -> dict:
    """small_run이 모은 카운터를 최종 리포트로 조립하고 내보낸다."""
    report = build_report(store)
    report["mode"] = "targeted"
    report["run_id"] = run_id
    # stored_records는 DB 전체 누계다. "이번 실행이 몇 건 저장했나"는 따로 실어야
    # 화면이 누계를 실행 성과로 잘못 읽지 않는다.
    report["run_stored"] = int(sum(collected.values()))
    report["run_candidates"] = int(sum(s.get("candidate_count", 0) for s in prov_stats.values()))
    # 후보 0건으로 끝난 LV2와 그 이유(가장 흔한 건 '이미 목표 도달'이라 실패가 아니다).
    report["skipped"] = skipped or {}
    report["coverage"] = _coverage_delta(targets, initial_cov, collected)
    report["provider_performance"] = _finalize_prov_stats(prov_stats)
    report["provider_usage"] = provider_usage
    report["by_query"] = [
        {"query": q, "lv2": qlv2.get(q, ""), **qmeta.get(q, {}), **s}
        for q, s in sorted(query_stats.items(), key=lambda kv: kv[1]["accepted"], reverse=True)
    ]
    # 계획별 누적 성과(실행 간 누적). 검색어 교체·source 우선순위 조정은 사람이 결정한다.
    report["query_plans"] = [
        {k: v for k, v in row.items() if k not in ("expected_korea_evidence", "expected_lv2_evidence")}
        for lv2 in strategy_lv2s
        for row in store.load_query_plans(lv2)
    ]
    if report_path:
        export_report(report, report_path)
    print_report(report)
    return report


def _coverage_delta(targets, initial_cov, collected) -> dict:
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


# ── 2) 수동 표본 검수 ──

_REVIEW_COLS = ("source_url", "title", "target_type", "source_id", "query_plan_id",
                "korea_evidence", "lv2_evidence", "published_at")


def sample_for_review(db_path: str, lv2: str, n: int = 20, seed: int = 0,
                      out_path: str | None = None) -> list[dict]:
    """수동 표본 검수용 표본. 본문 LLM 검증을 없앤 대가로 이 검수는 생략할 수 없다.

    합격선: domestic_direct precision >=95%, LV2 precision >=90%, combined >=85%.
    seed 고정 랜덤이라 같은 DB·seed면 같은 표본이 나온다(재검수 비교 가능).
    """
    store = Store(db_path)
    rows = [
        dict(zip((*_REVIEW_COLS, "body_excerpt"), row))
        for row in store.conn.execute(
            f"SELECT {','.join(_REVIEW_COLS)}, substr(COALESCE(core_text,body_text),1,300) "
            f"FROM content_records WHERE taxonomy_lv2=? AND action='accepted' "
            f"ORDER BY substr(content_id,1,8) || ? LIMIT ?", (lv2, str(seed), n))
    ]
    store.close()
    if out_path and rows:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    return rows


# ── 4) 재검수가 쓰는 본문 기준 판정 ──

def adjudicate(match, rec, target_lv2: str, p2: dict, deficit_of) -> tuple[str, str]:
    """본문 기준 최종 판정. korea_relevance 독립 게이트 + opportunistic mismatch."""
    acc = p2.get("acceptance", {})
    predicted = match.taxonomy_lv2
    korea = rec.korea_relevance_score or 0
    fit = rec.taxonomy_fit_score or 0
    concrete = rec.concrete_context_score or 0
    broad = bool(acc.get("broad_candidate", False))
    if not match.is_relevant:
        return "discard", "not_relevant"
    if broad and not rec.is_harmful:
        return "discard", "not_harmful"
    if korea < float(acc.get("min_korea_relevance_score", 0.6)):
        return "discard", "low_korea_relevance"
    if (fit >= float(acc.get("min_taxonomy_fit_score", 0.75))
            and concrete >= float(acc.get("min_concrete_context_score", 0.6))):
        action = "accepted"
    else:
        return "discard", "low_taxonomy_fit"
    if predicted != target_lv2:   # opportunistic: 자체 accepted 등급 + predicted 부족일 때만
        allow = p2.get("adjudication", {}).get("allow_opportunistic_accept", True)
        if action == "accepted" and allow and deficit_of(predicted) > 0:
            return "accepted", f"opportunistic:{target_lv2}->{predicted}"
        return "discard", f"mismatch_not_qualified:{target_lv2}->{predicted}"
    return action, "broad_candidate" if broad else "matched"


# ── 3) 사후 OpenAI 재검수 ──

def verify_unverified_candidates(run_id: str, db_path: str, config_path: str,
                                 taxonomy_config: str = "configs/taxonomy.yaml",
                                 settings_config: str = "configs/crawler_settings.yaml",
                                 target_lv2: str | None = None, limit: int = 60,
                                 overrides: dict | None = None) -> dict:
    """기존 Tavily 미검수 후보의 저장 본문만 OpenAI로 재분류한다. Tavily/fetch는 재호출하지 않는다."""
    with open(settings_config, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    p2 = apply_overrides(load_phase2_config(config_path), overrides)
    policies = load_policies(taxonomy_config)
    valid_pairs = build_taxonomy_index(policies)
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
               title,body_text,core_text,collected_at,search_query,search_api,extractor,run_id
        FROM content_records WHERE {' AND '.join(where)} ORDER BY rowid LIMIT ?
    """, (*params, int(limit))).fetchall()
    matcher_llm = build_llm(settings)
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
            body_text=row["body_text"] or "", core_text=row["core_text"] or "",
            collected_at=row["collected_at"] or "", search_query=row["search_query"] or "",
            search_api=row["search_api"] or "", extractor=row["extractor"] or "", content_id=row["content_id"],
            run_id=row["run_id"] or "", collection_phase=2,
        )
        match = matcher_llm.classify(
            rec, policies, valid_pairs,
            broad_candidate=bool(p2.get("acceptance", {}).get("broad_candidate", False)),
        )
        if match is None:
            stats["errors"] += 1
            continue
        rec.taxonomy_lv1 = match.taxonomy_lv1 or None
        rec.taxonomy_lv2 = match.taxonomy_lv2
        rec.category = rec.subtype = match.subtype
        action, reason = adjudicate(
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
        candidate_status = "accepted" if action == "accepted" else "discard"
        store.conn.execute("""
            UPDATE url_candidates SET status=?,filter_reason=?,llm_model=?,llm_input_tokens=?,
                llm_cached_input_tokens=?,llm_output_tokens=?,llm_total_tokens=?,llm_estimated_cost_usd=?
            WHERE run_id=? AND source_url=? AND status IN ('candidate','accepted')
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


