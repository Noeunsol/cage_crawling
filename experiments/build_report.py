"""test_experiment_runs.jsonl에 기록된 run들을 모아 정량 평가 표를 만든다.

python -m experiments.build_report
-> experiments/report.md, experiments/report.csv, experiments/report_failure_reasons.csv 생성.

사람 수동 수집 기록(experiments/human_baseline.csv)이 채워져 있으면 "1건당 시간(사람)" 비교
행도 report에 같이 붙는다.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.common import EXPERIMENT_DB_PATH, EXPERIMENT_LOG_PATH
from src.config.loader import load_all_configs
from src.storage.database import connect
from src.storage.repositories import runs as runs_repo

LOG_PATH = EXPERIMENT_LOG_PATH
HUMAN_BASELINE_PATH = Path(__file__).with_name("human_baseline.csv")
REPORT_MD_PATH = Path(__file__).with_name("report.md")
REPORT_CSV_PATH = Path(__file__).with_name("report.csv")
FAILURE_CSV_PATH = Path(__file__).with_name("report_failure_reasons.csv")

COLUMNS = [
    "LV2", "목표수집량", "배수", "accepted량", "목표달성률", "최종채택률", "수집실패량", "검색후보량",
    "tavily API 사용수", "serpapi API 사용수", "tavily후보기여율", "serpapi후보기여율",
    "tavily accepted기여율", "serpapi accepted기여율", "본문추출성공률", "중복률",
    "콘텐츠퀄리티(평균/5)", "고품질비율(4점이상)", "Taxonomy정밀도(라벨수)",
    "수집openai비용(USD)", "품질채점openai비용(USD)", "accepted당비용(USD)",
    "수집소요시간(초)", "accepted당소요시간(초)",
    "고유도메인수", "최다도메인비율",
]

# 성능(수집량)/비용/시간만 뽑은 요약표 — 나머지 상세 컬럼은 COLUMNS 전체 리포트에서 확인한다.
FOCUS_COLUMNS = [
    "LV2", "목표수집량", "accepted량", "목표달성률",
    "tavily API 사용수", "serpapi API 사용수", "수집openai비용(USD)", "품질채점openai비용(USD)",
    "수집소요시간(초)",
]

# discarded_candidates.reason 중 "본문을 실제로 fetch/extract 시도한 뒤" 결정되는 사유만.
# blacklisted_domain/duplicate(URL 완전일치)/timeout/access_denied/not_found/temporary_http_error는
# fetch 이전(또는 fetch 실패)에 걸러지므로 "추출 시도" 분모에서 제외한다 (retry_policy.yaml 참고).
_POST_EXTRACTION_REASONS = {
    "extraction_empty", "extraction_too_short", "date_out_of_range",
    "low_korea_relevance", "taxonomy_mismatch", "same_content_hash", "near_duplicate", "unexpected_error",
}
_EXTRACTION_FAILURE_REASONS = {"extraction_empty", "extraction_too_short"}
_DUPLICATE_REASONS = {"duplicate", "near_duplicate", "same_content_hash"}


def _runs_per_lv2() -> dict[str, list[tuple[str, int]]]:
    """LV2 하나가 여러 번(전체 수집 + type 보충 재실행 등) 나뉘어 돌았을 수 있어, 로그에 있는
    completed run 전부를 모아 합산한다 — 마지막 run 하나만 보면 그 전 run들의 accepted가 누락된다
    (2026-09-08, cyberstalking만 tavily로 보충 재실행했을 때 실제로 발견한 문제).
    (run_id, target_count) 쌍 그대로 돌려준다 — main()이 현재 DB에 없는 run_id를 걸러낸 *뒤에*
    target_count를 합산해야, 다른 실험 DB에서 온 run의 목표량이 섞여 들어가지 않는다."""
    runs_by_lv2: dict[str, list[tuple[str, int]]] = {}
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("status", "completed") != "completed":
            continue
        runs_by_lv2.setdefault(row["lv2_id"], []).append((row["run_id"], row["target_count"]))
    return runs_by_lv2


def _rate(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _openai_cost_usd(configs: dict, prompt_tokens: int, completion_tokens: int) -> float:
    openai_cfg = configs["providers"]["openai"]
    return (
        prompt_tokens * openai_cfg["input_price_per_1m_usd"] + completion_tokens * openai_cfg["output_price_per_1m_usd"]
    ) / 1_000_000


def _query_generation_cost_usd(configs: dict, prompt_tokens: int, completion_tokens: int) -> float:
    cfg = configs["providers"]["openai"]
    return (
        prompt_tokens * cfg["query_generation_input_price_per_1m_usd"]
        + completion_tokens * cfg["query_generation_output_price_per_1m_usd"]
    ) / 1_000_000


def build_row(conn, configs: dict, lv2_id: str, run_ids: list[str], target_count: int) -> dict:
    placeholders = ",".join("?" * len(run_ids))
    run_rows = [r for r in (runs_repo.get_run(conn, rid) for rid in run_ids) if r is not None]
    settings = next(
        (json.loads(r["settings_snapshot"]) for r in run_rows if r["settings_snapshot"]), {}
    )
    usage: dict[str, list] = {}
    for r in run_rows:
        if not r["provider_usage_summary"]:
            continue
        for provider, calls in json.loads(r["provider_usage_summary"]).items():
            usage.setdefault(provider, []).extend(calls)

    discoveries_count = conn.execute(
        f"SELECT COUNT(*) c FROM content_discoveries WHERE run_id IN ({placeholders})", run_ids
    ).fetchone()["c"]
    discarded_count = conn.execute(
        f"SELECT COUNT(*) c FROM discarded_candidates WHERE run_id IN ({placeholders})", run_ids
    ).fetchone()["c"]
    accepted_count = conn.execute(
        f"""
        SELECT COUNT(DISTINCT d.content_id) n
        FROM content_discoveries d
        JOIN search_queries sq ON sq.id = d.query_id
        JOIN content_taxonomy_mappings m
          ON m.content_id = d.content_id
         AND m.taxonomy_lv2 = sq.taxonomy_lv2 AND m.type_name = sq.type_name
        WHERE d.run_id IN ({placeholders}) AND m.decision = 'accepted'
        """,
        run_ids,
    ).fetchone()["n"]
    search_candidates = discoveries_count + discarded_count  # 검색이 실제로 찾아낸 후보 총수
    collection_failures = search_candidates - accepted_count  # accepted가 안 된 나머지 전부(제외+실패)

    tavily_calls = len(usage.get("tavily", []))
    serpapi_calls = len(usage.get("serpapi", []))

    discarded_reasons = conn.execute(
        f"SELECT reason, COUNT(*) c FROM discarded_candidates WHERE run_id IN ({placeholders}) GROUP BY reason",
        run_ids,
    ).fetchall()
    reason_counts = {r["reason"]: r["c"] for r in discarded_reasons}
    post_extraction_discarded = sum(c for reason, c in reason_counts.items() if reason in _POST_EXTRACTION_REASONS)
    extraction_attempted = accepted_count + post_extraction_discarded
    extraction_failed = sum(reason_counts.get(r, 0) for r in _EXTRACTION_FAILURE_REASONS)
    extraction_success_rate = _rate(extraction_attempted - extraction_failed, extraction_attempted)
    duplicate_count = sum(reason_counts.get(r, 0) for r in _DUPLICATE_REASONS)
    duplicate_rate = _rate(duplicate_count, search_candidates)

    # provider별 후보/accepted 기여율. 후보는 content_discoveries(추출까지 간 건)+discarded_candidates
    # (query_id로 provider 역추적) 합산, discarded 쪽에서 query_id가 없는 행(드묾)은 집계에서 빠진다.
    candidate_by_provider = dict(conn.execute(
        f"""
        SELECT provider, COUNT(*) c FROM (
            SELECT provider FROM content_discoveries WHERE run_id IN ({placeholders})
            UNION ALL
            SELECT sq.provider FROM discarded_candidates dc
            JOIN search_queries sq ON sq.id = dc.query_id
            WHERE dc.run_id IN ({placeholders})
        )
        GROUP BY provider
        """,
        run_ids + run_ids,
    ).fetchall())
    accepted_by_provider = dict(conn.execute(
        f"""
        SELECT d.provider, COUNT(DISTINCT d.content_id) n
        FROM content_discoveries d
        JOIN search_queries sq ON sq.id = d.query_id
        JOIN content_taxonomy_mappings m
          ON m.content_id = d.content_id
         AND m.taxonomy_lv2 = sq.taxonomy_lv2 AND m.type_name = sq.type_name
        WHERE d.run_id IN ({placeholders}) AND m.decision = 'accepted'
        GROUP BY d.provider
        """,
        run_ids,
    ).fetchall())
    total_candidates_by_provider = sum(candidate_by_provider.values())
    tavily_candidate_share = _rate(candidate_by_provider.get("tavily", 0), total_candidates_by_provider)
    serpapi_candidate_share = _rate(candidate_by_provider.get("serpapi", 0), total_candidates_by_provider)
    tavily_accepted_share = _rate(accepted_by_provider.get("tavily", 0), accepted_count)
    serpapi_accepted_share = _rate(accepted_by_provider.get("serpapi", 0), accepted_count)

    domain_counts = conn.execute(
        f"""
        SELECT c.source_domain, COUNT(DISTINCT c.id) n
        FROM content_discoveries d
        JOIN contents c ON c.id = d.content_id
        JOIN search_queries sq ON sq.id = d.query_id
        JOIN content_taxonomy_mappings m
          ON m.content_id = c.id
         AND m.taxonomy_lv2 = sq.taxonomy_lv2 AND m.type_name = sq.type_name
        WHERE d.run_id IN ({placeholders}) AND m.decision = 'accepted'
        GROUP BY c.source_domain
        """,
        run_ids,
    ).fetchall()
    unique_domain_count = len(domain_counts)
    top_domain_share = _rate(max((r["n"] for r in domain_counts), default=0), accepted_count)

    taxonomy_prompt_tokens = sum(c["prompt_tokens"] for c in usage.get("openai", []))
    taxonomy_completion_tokens = sum(c["completion_tokens"] for c in usage.get("openai", []))
    query_usage = conn.execute(
        f"""
        SELECT COALESCE(SUM(prompt_tokens), 0) prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) completion_tokens,
               COALESCE(SUM(web_search_calls), 0) web_search_calls
        FROM query_generation_calls WHERE run_id IN ({placeholders})
        """,
        run_ids,
    ).fetchone()

    quality_rows = conn.execute(
        f"""
        SELECT overall, prompt_tokens, completion_tokens FROM content_quality_scores
        WHERE run_id IN ({placeholders}) AND taxonomy_lv2 = ?
        """,
        run_ids + [lv2_id],
    ).fetchall()
    quality_avg = round(sum(r["overall"] for r in quality_rows) / len(quality_rows), 2) if quality_rows else None
    high_quality_ratio = _rate(sum(1 for r in quality_rows if r["overall"] >= 4), len(quality_rows))
    quality_prompt_tokens = sum(r["prompt_tokens"] for r in quality_rows)
    quality_completion_tokens = sum(r["completion_tokens"] for r in quality_rows)

    labels = conn.execute(
        f"SELECT human_label FROM eval_labels WHERE run_id IN ({placeholders}) AND taxonomy_lv2 = ?",
        run_ids + [lv2_id],
    ).fetchall()
    labeled_total = len(labels)
    # eval_labels는 accepted 표본만 뽑으므로, 사람이 accepted로 동의한 비율 = precision.
    precision_match = sum(1 for r in labels if r["human_label"] == "accepted")
    precision_str = f"{_rate(precision_match, labeled_total)} ({labeled_total}건)" if labeled_total else "미라벨링"

    # 수집 비용(taxonomy 필터 + 검색어 생성 + fresh_vocabulary 웹서치)과 품질채점 비용(score_quality.py)은
    # 서로 다른 실험 단계이므로 분리해서 계산한다 — 합치면 "수집 자체가 얼마나 드는가"를 알 수 없다.
    collection_openai_cost = round(
        _openai_cost_usd(configs, taxonomy_prompt_tokens, taxonomy_completion_tokens)
        + _query_generation_cost_usd(configs, query_usage["prompt_tokens"], query_usage["completion_tokens"])
        + query_usage["web_search_calls"] * configs["providers"]["openai"]["web_search_price_per_1k_calls_usd"] / 1000,
        5,
    )
    quality_openai_cost = round(_openai_cost_usd(configs, quality_prompt_tokens, quality_completion_tokens), 5)
    elapsed = sum(runs_repo.elapsed_seconds(r) for r in run_rows)

    return {
        "LV2": lv2_id,
        "목표수집량": target_count,
        "배수": settings.get("candidate_multiplier"),
        "accepted량": accepted_count,
        "목표달성률": _rate(accepted_count, target_count),
        "최종채택률": _rate(accepted_count, search_candidates),
        "수집실패량": collection_failures,
        "검색후보량": search_candidates,
        "tavily API 사용수": tavily_calls,
        "serpapi API 사용수": serpapi_calls,
        "tavily후보기여율": tavily_candidate_share,
        "serpapi후보기여율": serpapi_candidate_share,
        "tavily accepted기여율": tavily_accepted_share,
        "serpapi accepted기여율": serpapi_accepted_share,
        "본문추출성공률": extraction_success_rate,
        "중복률": duplicate_rate,
        "콘텐츠퀄리티(평균/5)": quality_avg if quality_avg is not None else "미채점",
        "고품질비율(4점이상)": high_quality_ratio if high_quality_ratio is not None else "미채점",
        "Taxonomy정밀도(라벨수)": precision_str,
        "수집openai비용(USD)": collection_openai_cost,
        "품질채점openai비용(USD)": quality_openai_cost,
        "accepted당비용(USD)": _rate(collection_openai_cost, accepted_count),
        "수집소요시간(초)": round(elapsed, 1),
        "accepted당소요시간(초)": _rate(round(elapsed, 1), accepted_count),
        "고유도메인수": unique_domain_count,
        "최다도메인비율": top_domain_share,
        "_discarded_reasons": discarded_reasons,
        "_search_candidates": search_candidates,
    }


def _load_human_baseline() -> list[dict]:
    if not HUMAN_BASELINE_PATH.exists():
        return []
    with HUMAN_BASELINE_PATH.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> None:
    if not LOG_PATH.exists():
        print(f"{LOG_PATH}가 없습니다 — 먼저 run_experiment.py로 실험을 실행하세요.")
        return

    configs = load_all_configs()
    conn = connect(EXPERIMENT_DB_PATH)

    rows = []
    for lv2, entries in _runs_per_lv2().items():
        # LOG_PATH는 여러 실험 DB에 걸쳐 누적되는 로그라, 지금 연결된 EXPERIMENT_DB_PATH에는
        # 없는(다른 db에서 실행된) run_id가 섞여 있을 수 있다 — 그런 run_id와 그 목표량은 걸러낸다.
        found = [(rid, tc) for rid, tc in entries if runs_repo.get_run(conn, rid) is not None]
        missing = len(entries) - len(found)
        if missing:
            print(f"[skip] {lv2}: run_id {missing}개가 {EXPERIMENT_DB_PATH}에 없음 (다른 실험 DB의 기록)")
        if not found:
            continue
        found_run_ids = [rid for rid, _ in found]
        # LV2 목표는 초기 전체 수집 run의 target_count다 — type 보충 재실행은 대개 더 작은
        # target_count로 돌기 때문에 합산하면 목표가 재실행 횟수만큼 부풀어 오른다(2026-09-09).
        target_count = max(tc for _, tc in found)
        rows.append(build_row(conn, configs, lv2, found_run_ids, target_count))
    rows.sort(key=lambda r: r["LV2"])

    with REPORT_CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r[k] for k in COLUMNS})

    with FAILURE_CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["LV2", "reason", "count", "비율"])
        for r in rows:
            for reason_row in r["_discarded_reasons"]:
                writer.writerow([
                    r["LV2"], reason_row["reason"], reason_row["c"],
                    _rate(reason_row["c"], r["_search_candidates"]),
                ])

    human_rows = _load_human_baseline()
    md_lines = [
        "# 정량 평가 결과", "",
        "## 요약 (성능/비용/시간)", "",
        "| " + " | ".join(FOCUS_COLUMNS) + " |", "|" + "---|" * len(FOCUS_COLUMNS),
    ]
    for r in rows:
        md_lines.append("| " + " | ".join(str(r[c]) for c in FOCUS_COLUMNS) + " |")
    md_lines += ["", "## 전체 상세", "", "| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for r in rows:
        md_lines.append("| " + " | ".join(str(r[c]) for c in COLUMNS) + " |")
    if human_rows:
        md_lines += ["", "## 사람 수동 수집 비교 (experiments/human_baseline.csv)",
                      "", "| LV2 | 건수 | 총 소요시간(초) | 1건당시간(초) |", "|---|---|---|---|"]
        for hr in human_rows:
            count = int(hr["건수"]) if hr.get("건수") else 0
            seconds = float(hr["총_소요시간_초"]) if hr.get("총_소요시간_초") else 0
            per_item = round(seconds / count, 1) if count else "-"
            md_lines.append(f"| {hr['LV2']} | {count} | {seconds} | {per_item} |")
    else:
        md_lines += ["", f"_사람 대조군 기록 없음 — {HUMAN_BASELINE_PATH.name}를 채우면 비교표가 추가됩니다._"]

    REPORT_MD_PATH.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"{len(rows)}개 LV2 결과를 {REPORT_MD_PATH.name}, {REPORT_CSV_PATH.name}에 저장했습니다.")


if __name__ == "__main__":
    main()
