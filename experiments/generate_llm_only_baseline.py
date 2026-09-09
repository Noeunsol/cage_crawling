"""Source-free LLM baseline context를 type별 CSV로 생성한다.

python -m experiments.generate_llm_only_baseline --confirm
python -m experiments.generate_llm_only_baseline --confirm --lv2 1_A_Toxic_Language
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import AsyncOpenAI

from src.config.loader import PROJECT_ROOT, load_all_configs
from src.utils.prompts import call_structured_output_async, load_prompt
from src.utils.text import compute_content_hash

MODEL = "gpt-4o-mini"
TARGET_REGION = "South Korea"
OUTPUT_DIR = PROJECT_ROOT / "data" / "llm_only"
CSV_COLUMNS = [
    "title", "content", "context_kind", "lv2_category", "type_name",
    "generation_model", "prompt_version",
]


def allocate_counts(total: int, type_count: int) -> list[int]:
    """total을 type 순서대로 round-robin 배분한다."""
    if total < 0 or type_count < 1:
        raise ValueError("total은 0 이상, type_count는 1 이상이어야 합니다.")
    quotient, remainder = divmod(total, type_count)
    return [quotient + (index < remainder) for index in range(type_count)]


def _criteria(items: list[str]) -> str:
    return "\n".join(f"  - {item}" for item in items) or "  (지정된 기준 없음)"


def unique_contexts(contexts: list[dict], seen: set[str], limit: int | None = None) -> list[dict]:
    accepted = []
    for context in contexts:
        title = context.get("title", "").strip()
        content = context.get("content", "").strip()
        if not title or not content:
            continue
        content_hash = compute_content_hash(title, content)
        if content_hash in seen:
            continue
        seen.add(content_hash)
        accepted.append({**context, "title": title, "content": content})
        if limit is not None and len(accepted) == limit:
            break
    return accepted


async def generate_type(
    client, prompt_cfg: dict, lv2: dict, type_cfg: dict, target: int, *,
    batch_size: int, max_retries: int, seen: set[str], call=call_structured_output_async,
) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "elapsed_s": 0.0}
    # max_retries는 초기 필요 batch 호출 후, 중복으로 부족할 때의 추가 호출 횟수다.
    max_calls = (target + batch_size - 1) // batch_size + max_retries
    while len(rows) < target and usage["calls"] < max_calls:
        count = min(batch_size, target - len(rows))
        result = await call(
            client, prompt_cfg, MODEL,
            lv2_category=lv2["lv2_name"], type_name=type_cfg["name"],
            definition=type_cfg["definition"],
            include_criteria=_criteria(type_cfg.get("include_criteria") or []),
            exclude_criteria=_criteria(type_cfg.get("exclude_criteria") or []),
            target_region=TARGET_REGION, count=str(count),
        )
        usage["calls"] += 1
        for key in ("prompt_tokens", "completion_tokens", "elapsed_s"):
            usage[key] += getattr(result, key)
        rows.extend(unique_contexts(result.data["contexts"], seen, target - len(rows)))
    return rows, usage


def write_csv(path: Path, rows: list[dict], lv2_id: str, type_name: str, prompt_version: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows({
            **row, "lv2_category": lv2_id, "type_name": type_name,
            "generation_model": MODEL, "prompt_version": prompt_version,
        } for row in rows)
    temporary.replace(path)


async def run(args: argparse.Namespace) -> dict:
    configs = load_all_configs()
    prompt_cfg = load_prompt("llm_only_context_generation")
    api_key_env = configs["providers"]["openai"]["api_key_env"]
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"{api_key_env} 환경변수가 설정되지 않았습니다.")
    client = AsyncOpenAI(api_key=api_key)
    groups = [
        g for g in configs["taxonomy"]["taxonomy"]
        if (not args.lv2 or g["lv2_id"] == args.lv2)
        and (not args.type_name or any(t["name"] == args.type_name for t in g["types"]))
    ]
    if not groups:
        raise ValueError(f"Taxonomy 대상을 찾을 수 없습니다: LV2={args.lv2}, type={args.type_name}")

    started = time.monotonic()
    seen: set[str] = set()
    summary = {
        "model": MODEL, "prompt_name": prompt_cfg["name"], "prompt_version": prompt_cfg["version"],
        "target_per_category": args.target_per_category, "batch_size": args.batch_size,
        "max_retries": args.max_retries, "seed": None, "categories": [],
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "api_elapsed_s": 0.0,
    }
    shortages = []
    for lv2 in groups:
        types = [
            t for t in lv2["types"]
            if t.get("enabled", True) and (not args.type_name or t["name"] == args.type_name)
        ]
        allocations = allocate_counts(args.target_per_category, len(types))
        category_generated = 0
        for type_cfg, target in zip(types, allocations):
            rows, usage = await generate_type(
                client, prompt_cfg, lv2, type_cfg, target,
                batch_size=args.batch_size, max_retries=args.max_retries, seen=seen,
            )
            write_csv(
                args.output_dir / lv2["lv2_id"] / f"{type_cfg['name']}.csv", rows,
                lv2["lv2_id"], type_cfg["name"], prompt_cfg["version"],
            )
            category_generated += len(rows)
            for key in ("calls", "prompt_tokens", "completion_tokens"):
                summary[key] += usage[key]
            summary["api_elapsed_s"] += usage["elapsed_s"]
            if len(rows) < target:
                shortages.append(f"{lv2['lv2_id']}/{type_cfg['name']}: {len(rows)}/{target}")
        summary["categories"].append({
            "lv2_id": lv2["lv2_id"], "target": args.target_per_category, "generated": category_generated,
        })

    prices = configs["providers"]["openai"]
    summary["estimated_cost_usd"] = (
        summary["prompt_tokens"] * prices["input_price_per_1m_usd"]
        + summary["completion_tokens"] * prices["output_price_per_1m_usd"]
    ) / 1_000_000
    summary["wall_elapsed_s"] = time.monotonic() - started
    summary["shortages"] = shortages
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "generation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true", help="실제 OpenAI 호출(비용 발생)에 동의")
    parser.add_argument("--target-per-category", type=int, default=30)
    parser.add_argument("--batch-size", type=int, choices=range(3, 6), default=5)
    parser.add_argument("--max-retries", type=int, default=3, help="type별 기본 batch 호출 후 추가 시도 횟수")
    parser.add_argument("--lv2", help="특정 LV2만 생성")
    parser.add_argument("--type", dest="type_name", help="특정 type만 생성")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    if not args.confirm:
        print("OpenAI를 호출해 비용이 발생합니다. --confirm을 붙여 실행하세요.")
        return
    if args.target_per_category < 1 or args.max_retries < 0:
        parser.error("target-per-category는 1 이상, max-retries는 0 이상이어야 합니다.")
    summary = asyncio.run(run(args))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["shortages"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
