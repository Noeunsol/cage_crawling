"""Taxonomy 정밀도 평가용 사람 라벨링 워크플로 (LV2당 accepted 30건 랜덤 표본).

1) sample: test_experiment_runs.jsonl에 기록된 run들에서 LV2당 accepted 30건을 뽑아 CSV로 내보낸다.
   사람이 그 CSV의 human_label 칸(accepted/excluded)을 채운다.
2) import: 사람이 채운 CSV를 읽어 eval_labels 테이블에 저장한다.

사용법:
    python -m experiments.labeling sample --out experiments/labeling_sample.csv
    (CSV의 human_label 칸을 accepted/excluded로 채운 뒤)
    python -m experiments.labeling import --in experiments/labeling_sample.csv --labeled-by me
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.common import EXPERIMENT_DB_PATH, EXPERIMENT_LOG_PATH
from src.storage.database import connect

LOG_PATH = EXPERIMENT_LOG_PATH
SAMPLE_SIZE_PER_LV2 = 30
RANDOM_SEED = 42  # 재현 가능한 표본 추출용 고정 시드


def _latest_run_per_lv2() -> dict[str, str]:
    """test_experiment_runs.jsonl에서 lv2_id별 가장 최근 run_id만 남긴다."""
    latest: dict[str, str] = {}
    if not LOG_PATH.exists():
        return latest
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("status", "completed") != "completed":
            continue
        latest[row["lv2_id"]] = row["run_id"]
    return latest


def _connect():
    return connect(EXPERIMENT_DB_PATH)


def cmd_sample(args: argparse.Namespace) -> None:
    conn = _connect()
    rng = random.Random(RANDOM_SEED)
    run_by_lv2 = _latest_run_per_lv2()
    if not run_by_lv2:
        print(f"{LOG_PATH}가 비어있습니다 — 먼저 run_experiment.py를 실행하세요.")
        return

    rows_out: list[dict] = []
    for lv2_id, run_id in run_by_lv2.items():
        accepted = conn.execute(
            """
            SELECT DISTINCT c.id AS content_id, m.type_name, c.title, c.canonical_url,
                   c.source_domain, c.published_date
            FROM content_discoveries d
            JOIN contents c ON c.id = d.content_id
            JOIN search_queries sq ON sq.id = d.query_id
            JOIN content_taxonomy_mappings m
              ON m.content_id = c.id AND m.taxonomy_lv2 = sq.taxonomy_lv2 AND m.type_name = sq.type_name
            WHERE d.run_id = ? AND sq.taxonomy_lv2 = ? AND m.decision = 'accepted'
            """,
            (run_id, lv2_id),
        ).fetchall()
        sample = rng.sample(accepted, min(SAMPLE_SIZE_PER_LV2, len(accepted)))
        if len(accepted) < SAMPLE_SIZE_PER_LV2:
            print(f"[경고] {lv2_id}: accepted {len(accepted)}건뿐 (목표 {SAMPLE_SIZE_PER_LV2}건) — 있는 만큼만 표본 추출")
        for row in sample:
            rows_out.append({
                "run_id": run_id, "content_id": row["content_id"],
                "taxonomy_lv2": lv2_id, "type_name": row["type_name"],
                "title": row["title"], "canonical_url": row["canonical_url"],
                "source_domain": row["source_domain"], "published_date": row["published_date"],
                "human_label": "",  # accepted / excluded 를 직접 채워넣기
            })

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()) if rows_out else [])
        writer.writeheader()
        writer.writerows(rows_out)
    print(f"{len(rows_out)}건을 {out_path}에 저장했습니다. human_label 칸을 accepted/excluded로 채워주세요.")


def cmd_import(args: argparse.Namespace) -> None:
    conn = _connect()
    in_path = Path(args.input)
    inserted = 0
    with in_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            label = row["human_label"].strip()
            if label not in ("accepted", "excluded"):
                print(f"[skip] content_id={row['content_id']}: human_label이 비어있거나 잘못됨 ({label!r})")
                continue
            with conn:
                conn.execute(
                    """
                    INSERT INTO eval_labels (run_id, content_id, taxonomy_lv2, type_name, human_label, labeled_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (run_id, content_id, taxonomy_lv2, type_name)
                    DO UPDATE SET human_label = excluded.human_label, labeled_by = excluded.labeled_by,
                                  labeled_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    """,
                    (row["run_id"], row["content_id"], row["taxonomy_lv2"], row["type_name"], label, args.labeled_by),
                )
            inserted += 1
    print(f"{inserted}건을 eval_labels에 저장했습니다.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_sample = sub.add_parser("sample", help="라벨링용 표본 CSV 생성")
    p_sample.add_argument("--out", default="experiments/labeling_sample.csv")
    p_sample.set_defaults(func=cmd_sample)

    p_import = sub.add_parser("import", help="채워진 CSV를 eval_labels에 저장")
    p_import.add_argument("--in", dest="input", required=True)
    p_import.add_argument("--labeled-by", required=True)
    p_import.set_defaults(func=cmd_import)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
