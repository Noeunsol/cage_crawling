"""test_experiment_runs.jsonl의 run들에서 accepted 콘텐츠 품질을 OpenAI로 점수화한다.

이미 accepted인 콘텐츠에 1~5점 품질 점수(타입적합성/한국 현지성/구체성/악용 각도/종합 + 문제
태그)를 매겨서 content_quality_scores에 저장한다. 2차 검증 게이트(2026-09-08): overall이
_MIN_ACCEPTED_OVERALL 미만이면 content_taxonomy_mappings.decision을 'excluded'로 내려
최종 accepted에서 빠지게 한다 — 목표 미달분을 자동으로 재수집하진 않는다(별도 결정 필요).
asyncio로 동시 호출해서 수백 건도 빠르게 처리한다.

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

from experiments.common import EXPERIMENT_CSV_DIR, EXPERIMENT_DB_PATH, EXPERIMENT_LOG_PATH
from src.config.loader import load_all_configs
from src.storage import csv_exporter
from src.storage.database import connect
from src.utils.prompts import call_structured_output_async, load_prompt

LOG_PATH = EXPERIMENT_LOG_PATH


def _runs_per_lv2() -> list[tuple[str, str]]:
    """(lv2_id, run_id) 전체 completed run 목록 — LV2 하나가 여러 번(전체 수집 + type 보충
    재실행 등) 나뉘어 돌았을 수 있어 마지막 run만 보면 그 전 run들의 accepted가 채점 대상에서
    누락된다 (2026-09-08, build_report.py에서 먼저 발견한 것과 같은 문제)."""
    runs: list[tuple[str, str]] = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("status", "completed") != "completed":
            continue
        runs.append((row["lv2_id"], row["run_id"]))
    return runs


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


_MIN_ACCEPTED_OVERALL = 2.5  # 2차 검증 게이트: 이 미만이면 accepted를 취소한다 (2026-09-08 결정,
# overall이 소수점을 갖게 되면서 3 -> 2.5로 조정 — type_relevance=2(주변부 관련)와 1(사실상 무관)을
# overall 산정에서 구분하기 시작했기 때문. quality_score.yaml 참고.


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
    # overall은 4항목 평균이 아니라 모델이 type_relevance 우선 규칙으로 직접 산정한다(prompts/quality_score.yaml).
    with conn:
        conn.execute(
            """
            INSERT INTO content_quality_scores
                (run_id, content_id, taxonomy_lv2, type_name, type_relevance,
                 korean_locality, specificity, injection_suitability, overall, reasoning, issues, model,
                 prompt_tokens, completion_tokens, elapsed_s)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["run_id"], row["content_id"], lv2_id, row["type_name"], data["type_relevance"],
             data["korean_locality"], data["specificity"], data["injection_suitability"], data["overall"],
             data["reasoning"], json.dumps(data["issues"], ensure_ascii=False), model,
             result.prompt_tokens, result.completion_tokens, result.elapsed_s),
        )
        # 2차 검증 게이트: quality_score overall이 기준 미만이면 accepted를 취소한다 (자동 재수집은 안 함, 별도 결정 필요).
        if data["overall"] < _MIN_ACCEPTED_OVERALL:
            conn.execute(
                """
                UPDATE content_taxonomy_mappings SET decision = 'excluded',
                    decision_reason = ?
                WHERE content_id = ? AND taxonomy_lv2 = ? AND type_name = ? AND decision = 'accepted'
                """,
                (f"quality_score_overall={data['overall']}(<{_MIN_ACCEPTED_OVERALL})",
                 row["content_id"], lv2_id, row["type_name"]),
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
    for lv2_id, run_id in _runs_per_lv2():
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

        rejected = conn.execute(
            """
            SELECT COUNT(*) c FROM content_quality_scores
            WHERE run_id = ? AND taxonomy_lv2 = ? AND overall < ?
            """,
            (run_id, lv2_id, _MIN_ACCEPTED_OVERALL),
        ).fetchone()["c"]
        if rejected:
            print(f"    누적 {rejected}건이 overall<{_MIN_ACCEPTED_OVERALL}으로 accepted 취소됨")
            # export_run()은 기존 CSV에 이어붙이기만 해서 취소된 행을 못 지운다 — 영향받은
            # type만 DB 기준으로 통째로 다시 써서 최종 CSV에 취소 반영.
            for type_name in {row["type_name"] for row in todo}:
                csv_exporter.export_type(conn, lv2_id, type_name, EXPERIMENT_CSV_DIR)

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
