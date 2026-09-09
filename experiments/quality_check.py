"""data/eacl/**/*.csv 사후 품질 체크 — 크롤러/DB와 완전히 분리해서 CSV만 보고 평가한다.

각 행(title/content)에 Taxonomy Alignment / Korean Context Grounding / Context
Meaningfulness(1~3점) + issue_tags + evidence/reason을 매겨 같은 CSV에 quality_* 컬럼으로
덧붙인다. 이미 평가된 행(quality_taxonomy_alignment 값이 있는 행)은 건너뛰므로 재실행해도
중복 과금되지 않는다. accepted 여부 등 어떤 판정도 바꾸지 않는다 — 순수 측정 전용.

python -m experiments.quality_check --confirm
python -m experiments.quality_check --confirm --limit 5    # 파일당 앞 5행만(저렴 테스트)
python -m experiments.quality_check --confirm --workers 8
python -m experiments.quality_check --confirm --file data/eacl/3_H_Prohibited_Advisory/legal_advice.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import AsyncOpenAI

from src.config.loader import PROJECT_ROOT, load_all_configs
from src.utils.prompts import call_structured_output_async, load_prompt

CSV_DIR = PROJECT_ROOT / "data" / "eacl"
NEW_COLUMNS = [
    "quality_taxonomy_alignment",
    "quality_korean_context_grounding",
    "quality_context_meaningfulness",
    "quality_issue_tags",
    "quality_eval_detail",  # JSON: evidence/reason per dimension + context_elements (감사용)
]


def _build_async_client(configs: dict) -> AsyncOpenAI:
    env_name = configs["providers"]["openai"]["api_key_env"]
    api_key = os.environ.get(env_name)
    if not api_key:
        raise RuntimeError(f"{env_name} 환경변수가 설정되지 않았습니다.")
    return AsyncOpenAI(api_key=api_key)


def _find_type_info(configs: dict, lv2_id: str, type_name: str) -> dict:
    for group in configs["taxonomy"]["taxonomy"]:
        if group["lv2_id"] == lv2_id:
            for t in group["types"]:
                if t["name"] == type_name:
                    return {
                        "definition": t["definition"],
                        "include_criteria": t.get("include_criteria") or [],
                        "exclude_criteria": t.get("exclude_criteria") or [],
                    }
    raise ValueError(f"configs/taxonomy.yaml에 {lv2_id}/{type_name}이 없습니다.")


async def _eval_row(client, prompt_cfg, model, type_info, type_name, row, semaphore) -> str | None:
    """성공하면 row에 quality_* 컬럼을 채우고 None을, 실패하면 사유 문자열을 돌려준다."""
    async with semaphore:
        try:
            result = await call_structured_output_async(
                client, prompt_cfg, model,
                title=row["title"], content=row["content"], type_name=type_name,
                definition=type_info["definition"],
                include_criteria="\n".join(f"- {c}" for c in type_info["include_criteria"]) or "(없음)",
                exclude_criteria="\n".join(f"- {c}" for c in type_info["exclude_criteria"]) or "(없음)",
            )
        except Exception as e:  # noqa: BLE001 - 한 건 실패로 나머지 동시 호출까지 취소되면 안 된다
            return f"{type(e).__name__}: {str(e)[:150]}"

    data = result.data
    row["quality_taxonomy_alignment"] = data["taxonomy_alignment"]["score"]
    row["quality_korean_context_grounding"] = data["korean_context_grounding"]["score"]
    row["quality_context_meaningfulness"] = data["context_meaningfulness"]["score"]
    row["quality_issue_tags"] = json.dumps(data["issue_tags"], ensure_ascii=False)
    row["quality_eval_detail"] = json.dumps(
        {
            "taxonomy_alignment": {k: data["taxonomy_alignment"][k] for k in ("evidence", "reason")},
            "korean_context_grounding": {k: data["korean_context_grounding"][k] for k in ("evidence", "reason")},
            "context_meaningfulness": {k: data["context_meaningfulness"][k] for k in ("evidence", "reason")},
            "context_elements": data["context_elements"],
        },
        ensure_ascii=False,
    )
    return None


async def process_file(client, prompt_cfg, model, configs, path: Path, limit: int | None, semaphore) -> None:
    path = path.resolve()
    display_path = path.relative_to(CSV_DIR) if path.is_relative_to(CSV_DIR) else path
    lv2_id = path.parent.name
    type_name = path.stem
    type_info = _find_type_info(configs, lv2_id, type_name)

    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    todo = [row for row in rows if not row.get("quality_taxonomy_alignment")]
    if limit is not None:
        todo = todo[:limit]
    if not todo:
        print(f"  {display_path}: 평가할 신규 행 없음 ({len(rows)}행)")
        return

    results = await asyncio.gather(*[
        _eval_row(client, prompt_cfg, model, type_info, type_name, row, semaphore) for row in todo
    ])
    failures = [r for r in results if r is not None]
    print(
        f"  {display_path}: {len(todo)}행 시도 -> "
        f"{len(todo) - len(failures)}행 평가 성공, {len(failures)}행 실패"
    )
    for f in failures[:5]:
        print(f"    실패 예: {f}")

    fieldnames = list(rows[0].keys()) if rows else []
    for col in NEW_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
    temporary_path = path.with_suffix(".csv.tmp")
    with open(temporary_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary_path.replace(path)


async def run(args: argparse.Namespace) -> None:
    configs = load_all_configs()
    client = _build_async_client(configs)
    prompt_cfg = load_prompt("quality_check")
    model = configs["providers"]["openai"]["model"]
    semaphore = asyncio.Semaphore(args.workers)

    paths = [Path(args.file)] if args.file else sorted(CSV_DIR.glob("*/*.csv"))
    for path in paths:
        await process_file(client, prompt_cfg, model, configs, path, args.limit, semaphore)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true", help="실제 OpenAI 호출(비용 발생)에 동의")
    parser.add_argument("--limit", type=int, default=None, help="파일당 앞 N행만 평가(저렴 테스트용)")
    parser.add_argument("--workers", type=int, default=5, help="동시 처리 수 (기본 5, rate limit 고려)")
    parser.add_argument("--file", type=str, default=None, help="특정 CSV 하나만 평가 (기본: data/eacl/*/*.csv 전체)")
    args = parser.parse_args()

    if not args.confirm:
        print("OpenAI를 호출해 비용이 발생합니다. 동의하면 --confirm을 붙여 다시 실행하세요.")
        return

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
