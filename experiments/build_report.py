"""experiment_runs.jsonl에 기록된 run들을 모아 정량 평가 표(LV2별 12개 지표)를 만든다.

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

from src.config.loader import PROJECT_ROOT, load_all_configs
from src.storage.database import connect
from src.storage.repositories import runs as runs_repo

LOG_PATH = Path(__file__).with_name("experiment_runs.jsonl")
HUMAN_BASELINE_PATH = Path(__file__).with_name("human_baseline.csv")
REPORT_MD_PATH = Path(__file__).with_name("report.md")
REPORT_CSV_PATH = Path(__file__).with_name("report.csv")
FAILURE_CSV_PATH = Path(__file__).with_name("report_failure_reasons.csv")

COLUMNS = [
    "LV2", "목표수집량", "배수", "accepted량", "목표달성률", "수집실패량", "검색후보량",
    "tavily API 사용수", "serpapi API 사용수", "콘텐츠퀄리티(평균/5)", "Taxonomy정밀도(라벨수)",
    "사용된openai비용(USD)", "수집소요시간(초)",
]


def _latest_run_per_lv2() -> list[tuple[str, str, int]]:
    latest: dict[str, tuple[str, int]] = {}
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        latest[row["lv2_id"]] = (row["run_id"], row["target_count"])
    return [(lv2, run_id, tc) for lv2, (run_id, tc) in latest.items()]


def _rate(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _openai_cost_usd(configs: dict, prompt_tokens: int, completion_tokens: int) -> float:
    openai_cfg = configs["providers"]["openai"]
    return (
        prompt_tokens * openai_cfg["input_price_per_1m_usd"] + completion_tokens * openai_cfg["output_price_per_1m_usd"]
    ) / 1_000_000


def build_row(conn, configs: dict, lv2_id: str, run_id: str, target_count: int) -> dict:
    run_row = runs_repo.get_run(conn, run_id)
    settings = json.loads(run_row["settings_snapshot"]) if run_row["settings_snapshot"] else {}
    usage = json.loads(run_row["provider_usage_summary"]) if run_row["provider_usage_summary"] else {}

    discoveries_count = conn.execute(
        "SELECT COUNT(*) c FROM content_discoveries WHERE run_id = ?", (run_id,)
    ).fetchone()["c"]
    discarded_count = conn.execute(
        "SELECT COUNT(*) c FROM discarded_candidates WHERE run_id = ?", (run_id,)
    ).fetchone()["c"]
    accepted_count = conn.execute(
        """
        SELECT COUNT(DISTINCT c.id) n FROM content_discoveries d
        JOIN contents c ON c.id = d.content_id
        WHERE d.run_id = ? AND c.status = 'accepted'
        """,
        (run_id,),
    ).fetchone()["n"]
    search_candidates = discoveries_count + discarded_count  # 검색이 실제로 찾아낸 후보 총수
    collection_failures = search_candidates - accepted_count  # accepted가 안 된 나머지 전부(제외+실패)

    tavily_calls = len(usage.get("tavily", []))
    serpapi_calls = len(usage.get("serpapi", []))

    taxonomy_prompt_tokens = sum(c["prompt_tokens"] for c in usage.get("openai", []))
    taxonomy_completion_tokens = sum(c["completion_tokens"] for c in usage.get("openai", []))

    quality_rows = conn.execute(
        """
        SELECT q.overall, q.prompt_tokens, q.completion_tokens FROM content_quality_scores q
        JOIN content_discoveries d ON d.content_id = q.content_id
        WHERE d.run_id = ? AND q.taxonomy_lv2 = ?
        """,
        (run_id, lv2_id),
    ).fetchall()
    quality_avg = round(sum(r["overall"] for r in quality_rows) / len(quality_rows), 2) if quality_rows else None
    quality_prompt_tokens = sum(r["prompt_tokens"] for r in quality_rows)
    quality_completion_tokens = sum(r["completion_tokens"] for r in quality_rows)

    labels = conn.execute(
        "SELECT human_label FROM eval_labels WHERE taxonomy_lv2 = ?", (lv2_id,)
    ).fetchall()
    labeled_total = len(labels)
    # eval_labels는 accepted 표본만 뽑으므로, 사람이 accepted로 동의한 비율 = precision.
    precision_match = sum(1 for r in labels if r["human_label"] == "accepted")
    precision_str = f"{_rate(precision_match, labeled_total)} ({labeled_total}건)" if labeled_total else "미라벨링"

    total_openai_cost = _openai_cost_usd(
        configs,
        taxonomy_prompt_tokens + quality_prompt_tokens,
        taxonomy_completion_tokens + quality_completion_tokens,
    )
    elapsed = runs_repo.elapsed_seconds(run_row)

    return {
        "LV2": lv2_id,
        "목표수집량": target_count,
        "배수": settings.get("candidate_multiplier"),
        "accepted량": accepted_count,
        "목표달성률": _rate(accepted_count, target_count),
        "수집실패량": collection_failures,
        "검색후보량": search_candidates,
        "tavily API 사용수": tavily_calls,
        "serpapi API 사용수": serpapi_calls,
        "콘텐츠퀄리티(평균/5)": quality_avg if quality_avg is not None else "미채점",
        "Taxonomy정밀도(라벨수)": precision_str,
        "사용된openai비용(USD)": round(total_openai_cost, 5),
        "수집소요시간(초)": round(elapsed, 1),
        "_discarded_reasons": conn.execute(
            "SELECT reason, COUNT(*) c FROM discarded_candidates WHERE run_id = ? GROUP BY reason", (run_id,)
        ).fetchall(),
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
    db_path = PROJECT_ROOT / configs["app"]["database"]["path"]
    conn = connect(db_path)

    rows = [build_row(conn, configs, lv2, run_id, tc) for lv2, run_id, tc in _latest_run_per_lv2()]
    rows.sort(key=lambda r: r["LV2"])

    with REPORT_CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r[k] for k in COLUMNS})

    with FAILURE_CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["LV2", "reason", "count"])
        for r in rows:
            for reason_row in r["_discarded_reasons"]:
                writer.writerow([r["LV2"], reason_row["reason"], reason_row["c"]])

    human_rows = _load_human_baseline()
    md_lines = ["# 정량 평가 결과", "", "| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
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
