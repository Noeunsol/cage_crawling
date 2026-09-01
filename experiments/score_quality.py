"""test_experiment_runs.jsonl의 run들에서 accepted 콘텐츠 품질을 OpenAI로 점수화한다.

채택 여부를 다시 판단하거나 걸러내지 않는다 — 이미 accepted인 콘텐츠에 1~5점 품질 점수(구체성/
정보성/적합도/한국 현지성/종합 + 문제 태그)만 매겨서 content_quality_scores에 저장한다
(성능 수치화 전용, 순수 추가 기록). asyncio로 동시 호출해서 수백 건도 빠르게 처리한다.

python -m experiments.score_quality --confirm
python -m experiments.score_quality --confirm --limit 5      # LV2당 앞 5건만(저렴 테스트)
python -m experiments.score_quality --confirm --workers 8    # 동시 처리 수 조정(기본 5)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import AsyncOpenAI

from experiments.common import EXPERIMENT_DB_PATH, EXPERIMENT_LOG_PATH
from src.config.loader import load_all_configs
from src.storage.database import connect
from src.utils.prompts import call_structured_output_async, load_prompt

LOG_PATH = EXPERIMENT_LOG_PATH


def _latest_run_per_lv2() -> dict[str, str]:
    latest: dict[str, str] = {}
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("status", "completed") != "completed":
            continue
        latest[row["lv2_id"]] = row["run_id"]
    return latest


def _find_type_def(configs: dict, lv2_id: str, type_name: str) -> str:
    for group in configs["taxonomy"]["taxonomy"]:
        if group["lv2_id"] == lv2_id:
            for t in group["types"]:
                if t["name"] == type_name:
                    return t["definition"]
    return ""


def _build_async_client(configs: dict) -> AsyncOpenAI:
    env_name = configs["providers"]["openai"]["api_key_env"]
    api_key = os.environ.get(env_name)
    if not api_key:
        raise RuntimeError(f"{env_name} 환경변수가 설정되지 않았습니다.")
    return AsyncOpenAI(api_key=api_key)


async def _score_one(conn, client, prompt_cfg, model, configs, lv2_id, row, semaphore) -> str | None:
    """성공하면 None, 실패하면 사유 문자열을 돌려준다 (한 건 실패로 나머지가 죽지 않게)."""
    async with semaphore:
        try:
            definition = _find_type_def(configs, lv2_id, row["type_name"])
            result = await call_structured_output_async(
                client, prompt_cfg, model,
                title=row["title"], content=row["content"], type_name=row["type_name"], definition=definition,
            )
        except Exception as e:  # noqa: BLE001 - 한 건 실패로 나머지 동시 호출까지 취소되면 안 된다
            return f"{type(e).__name__}: {str(e)[:150]}"

    data = result.data
    # 종합점수 = 네 항목(사례구체성/본문품질/Taxonomy관련성/한국관련성)의 평균 — 모델이 아니라 여기서 계산한다.
    overall = round(
        (data["specificity"] + data["content_quality"] + data["relevance_strength"] + data["korean_locality"]) / 4, 2
    )
    with conn:
        conn.execute(
            """
            INSERT INTO content_quality_scores
                (run_id, content_id, taxonomy_lv2, type_name, specificity, content_quality,
                 relevance_strength, korean_locality, overall, reason, issues, model,
                 prompt_tokens, completion_tokens, elapsed_s)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["run_id"], row["content_id"], lv2_id, row["type_name"], data["specificity"], data["content_quality"],
             data["relevance_strength"], data["korean_locality"], overall, data["reason"],
             json.dumps(data["issues"], ensure_ascii=False), model,
             result.prompt_tokens, result.completion_tokens, result.elapsed_s),
        )
    return None


async def run(args: argparse.Namespace) -> None:
    configs = load_all_configs()
    conn = connect(EXPERIMENT_DB_PATH)
    client = _build_async_client(configs)
    prompt_cfg = load_prompt("quality_score")
    model = configs["providers"]["openai"]["model"]
    semaphore = asyncio.Semaphore(args.workers)

    grand_total_scored = 0
    for lv2_id, run_id in _latest_run_per_lv2().items():
        accepted = conn.execute(
            """
            SELECT DISTINCT ? AS run_id, c.id AS content_id, m.type_name, c.title, c.content
            FROM content_discoveries d
            JOIN contents c ON c.id = d.content_id
            JOIN search_queries sq ON sq.id = d.query_id
            JOIN content_taxonomy_mappings m
              ON m.content_id = c.id AND m.taxonomy_lv2 = sq.taxonomy_lv2 AND m.type_name = sq.type_name
            WHERE d.run_id = ? AND sq.taxonomy_lv2 = ? AND m.decision = 'accepted'
            """,
            (run_id, run_id, lv2_id),
        ).fetchall()

        todo = [
            row for row in accepted
            if not conn.execute(
                """SELECT 1 FROM content_quality_scores
                   WHERE run_id = ? AND content_id = ? AND taxonomy_lv2 = ? AND type_name = ?""",
                (run_id, row["content_id"], lv2_id, row["type_name"]),
            ).fetchone()
        ]
        if args.limit is not None:
            todo = todo[: args.limit]
        if not todo:
            print(f"  {lv2_id}: 채점할 신규 건 없음 (accepted {len(accepted)}건)")
            continue

        results = await asyncio.gather(*[
            _score_one(conn, client, prompt_cfg, model, configs, lv2_id, row, semaphore) for row in todo
        ])
        failures = [f for f in results if f is not None]
        scored = len(todo) - len(failures)
        grand_total_scored += scored
        print(f"  {lv2_id}: {len(todo)}건 시도 -> {scored}건 채점 성공, {len(failures)}건 실패")
        for f in failures[:5]:
            print(f"    실패 예: {f}")

        avg_overall = conn.execute(
            """
            SELECT AVG(overall) a, COUNT(*) n FROM content_quality_scores
            WHERE run_id = ? AND taxonomy_lv2 = ?
            """,
            (run_id, lv2_id),
        ).fetchone()
        if avg_overall["n"]:
            print(f"    누적 평균 overall: {avg_overall['a']:.2f} ({avg_overall['n']}건)")

    print(f"완료: 총 {grand_total_scored}건 신규 채점.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true", help="실제 OpenAI 호출(비용 발생)에 동의")
    parser.add_argument("--limit", type=int, default=None, help="LV2당 앞 N건만 채점(저렴 테스트용)")
    parser.add_argument("--workers", type=int, default=5, help="동시 처리 수 (기본 5, rate limit 고려)")
    args = parser.parse_args()

    if not args.confirm:
        print("OpenAI를 호출해 비용이 발생합니다. 동의하면 --confirm을 붙여 다시 실행하세요.")
        return
    if not LOG_PATH.exists():
        print(f"{LOG_PATH}가 없습니다 — 먼저 run_experiment.py를 실행하세요.")
        return

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
