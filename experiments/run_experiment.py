"""정량 평가 실험용 run 실행기. LV2별로 target_count를 override해서 run_collection을 직접 호출한다.

Streamlit UI 없이 CLI로 실행한다 (UI가 유일한 실행 경로였기 때문). 실제 API를 호출해 비용이
발생하므로 --confirm을 명시해야 실행된다.

사용법:
    python -m experiments.run_experiment --target-count 30 --confirm            # 전체 19종 LV2
    python -m experiments.run_experiment --target-count 30 --lv2 1_A_Toxic_Language --confirm

각 run의 (lv2_id, run_id)는 experiments/experiment_runs.jsonl에 한 줄씩 append된다 —
build_report.py가 이 로그로 어떤 run을 집계할지 찾는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config.loader import PROJECT_ROOT, load_all_configs
from src.config.validator import validate_configs
from src.discovery.serpapi_provider import SerpApiProvider
from src.discovery.tavily_provider import TavilyProvider
from src.pipeline.collector import run_collection
from src.pipeline.preflight import run_preflight
from src.query import freshness, generator, repository, vocabulary, year_injection
from src.storage.repositories import fresh_vocabulary as fresh_vocab_repo
from src.storage.repositories import query_generation_calls as generation_calls_repo
from src.storage.database import connect
from src.storage.repositories import runs as runs_repo
from src.utils.prompts import load_prompt
from ui.common import (
    default_date_range, effective_date_range, effective_provider_ratio, find_type, suggested_query_counts,
    taxonomy_groups,
)

LOG_PATH = Path(__file__).with_name("experiment_runs.jsonl")
EXPERIMENT_TAG = "quant_eval"


def _provider_api_key(configs: dict, provider: str) -> str:
    return os.environ[configs["providers"][provider]["api_key_env"]]


def _lv2_targets(configs: dict, lv2_id: str) -> list[tuple[str, str]]:
    for group in taxonomy_groups(configs):
        if group["lv2_id"] == lv2_id:
            return [(lv2_id, t["name"]) for t in group["types"] if t.get("enabled", True)]
    return []


def ensure_queries(conn, configs: dict, targets: list[tuple[str, str]], setup: dict) -> None:
    """targets 중 active 검색어가 없는 (lv2, type, provider)만 골라 OpenAI로 생성해 채운다.

    query_review.py("3. 검색어 생성" 화면)와 동일한 로직 재사용 — UI 없이 이 부분만 필요해서 옮겼다.
    """
    missing = [
        (lv2, type_name, provider)
        for lv2, type_name in targets
        for provider in ("tavily", "serpapi")
        if not repository.list_active_queries(conn, taxonomy_lv2=lv2, type_name=type_name, provider=provider)
    ]
    if not missing:
        return

    client = generator.build_client(configs["providers"])
    prompt_cfg = load_prompt("query_generation")
    fresh_prompt_cfg = load_prompt("fresh_vocabulary")
    model = configs["providers"]["openai"]["model"]
    types_per_lv2 = Counter(lv2 for lv2, _ in targets)

    print(f"  검색어 없는 (LV2, type, provider) {len(missing)}건 자동 생성 중...")
    for lv2_id, type_name, provider in missing:
        type_cfg = find_type(configs, lv2_id, type_name)
        tavily_count, serpapi_count = suggested_query_counts(configs, setup, lv2_id, types_per_lv2[lv2_id])
        count = tavily_count if provider == "tavily" else serpapi_count
        if count <= 0:
            continue

        merged_vocabulary, _state, freshness_error = vocabulary.resolve_vocabulary(
            client=client, fresh_prompt_cfg=fresh_prompt_cfg, fresh_vocab_repo=fresh_vocab_repo,
            freshness_module=freshness, conn=conn, configs=configs,
            lv2_id=lv2_id, type_name=type_name, type_cfg=type_cfg,
        )
        if freshness_error is not None:
            print(f"    [경고] {lv2_id}::{type_name} 최근 표현 웹서치 실패 — 기존 vocabulary만 사용")

        result = generator.generate_queries(
            client, prompt_cfg, taxonomy_lv2=lv2_id, type_name=type_name,
            definition=type_cfg["definition"], search_vocabulary=merged_vocabulary,
            include_criteria=type_cfg["include_criteria"],
            exclude_criteria=vocabulary.effective_exclude_criteria(type_cfg),
            query_axes=type_cfg.get("query_axes", []), provider=provider,
            query_count=int(count), model=model,
            query_limits=configs["providers"].get("query_limits", {}),
        )
        repository.save_generated_queries(
            conn, taxonomy_lv2=lv2_id, type_name=type_name, provider=provider,
            query_texts=result.accepted, prompt_version=str(prompt_cfg["version"]), model=model,
        )
        generation_calls_repo.record_call(
            conn, taxonomy_lv2=lv2_id, type_name=type_name, provider=provider, model=model,
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
            elapsed_s=result.elapsed_s,
        )
        print(f"    {lv2_id}::{type_name}::{provider} -> {len(result.accepted)}개 생성")


def ensure_year_variants(conn, configs: dict, targets: list[tuple[str, str]], date_range_by_lv2: dict) -> None:
    """date_out_of_range 비율이 높은 (lv2,type,provider)에 한해 "원래 검색어 + 연도" 변형을 추가한다.

    상대적 날짜 표현("최근", "올해")은 여전히 금지 — 여기서 붙이는 건 실제 수집 기간에서 뽑은
    구체적 연도뿐이다 (src/query/year_injection.py). create_query가 이미 있는 검색어면 새로
    만들지 않으므로 매 run마다 불러도 안전하다.
    """
    query_limits = configs["providers"].get("query_limits", {})
    for lv2_id, type_name in targets:
        date_from, date_to = date_range_by_lv2[lv2_id]
        for provider in ("tavily", "serpapi"):
            if not year_injection.should_inject_year(conn, configs, lv2_id, type_name, provider):
                continue
            base_queries = repository.list_active_queries(
                conn, taxonomy_lv2=lv2_id, type_name=type_name, provider=provider
            )
            if not base_queries:
                continue
            limits = query_limits.get(provider, {})
            max_terms, max_chars = limits.get("max_terms"), limits.get("max_characters")

            def _fits(q: str) -> bool:
                if max_chars and len(q) > max_chars:
                    return False
                if max_terms and len(q.split()) > max_terms:
                    return False
                return True

            variants = [
                v for row in base_queries
                for v in year_injection.build_year_variants(row["query_text"], date_from, date_to)
                if _fits(v)
            ]
            if not variants:
                continue
            created = repository.save_generated_queries(
                conn, taxonomy_lv2=lv2_id, type_name=type_name, provider=provider,
                query_texts=variants, prompt_version="year_injection", model="none",
            )
            print(f"    [연도 변형] {lv2_id}::{type_name}::{provider} -> {len(created)}개 (date_out_of_range 높음)")


def run_one_lv2(
    conn, configs: dict, lv2_id: str, *,
    target_count: int, candidate_multiplier: float, use_adaptive_multiplier: bool = False,
) -> str | None:
    targets = _lv2_targets(configs, lv2_id)
    if not targets:
        print(f"[skip] {lv2_id}: 활성화된 type이 없음")
        return None

    date_from, date_to = default_date_range(configs["collection"]["defaults"]["date_range_years"])
    setup = {
        "date_overrides": {}, "provider_ratio_overrides": {},
        "date_from": date_from, "date_to": date_to,
        "provider_ratio": configs["collection"]["provider_ratio"]["default"],
        "target_count": target_count, "candidate_multiplier": candidate_multiplier,
    }
    date_range_by_lv2 = {lv2_id: effective_date_range(setup, configs, lv2_id)}
    provider_ratio_by_lv2 = {lv2_id: effective_provider_ratio(setup, configs, lv2_id)}

    ensure_queries(conn, configs, targets, setup)
    ensure_year_variants(conn, configs, targets, date_range_by_lv2)

    report = run_preflight(
        conn, configs, targets, target_count=target_count, candidate_multiplier=candidate_multiplier,
    )
    if not report.can_run:
        print(f"[skip] {lv2_id}: 사전 검사 실패 (missing_api_keys={report.missing_api_keys})")
        return None

    run_id = runs_repo.new_run_id()
    providers = {
        "tavily": TavilyProvider(_provider_api_key(configs, "tavily"), configs["providers"]["tavily"]),
        "serpapi": SerpApiProvider(_provider_api_key(configs, "serpapi"), configs["providers"]["serpapi"]),
    }
    openai_client = generator.build_client(configs["providers"])

    # (A) 수집 중 accepted/excluded를 가르는 OpenAI taxonomy 적합성 검사는 이 실험에서 끈다.
    # 품질은 수집 후 score_quality.py((B), accepted에 영향 없는 별도 점수화)로만 측정한다.
    run_configs = {**configs, "extraction": {**configs["extraction"], "taxonomy_filter": {"enabled": False}}}

    runs_repo.create_run(conn, run_id, {
        "target_count": target_count, "candidate_multiplier": candidate_multiplier,
        "targets": [f"{lv2}::{t}" for lv2, t in targets],
        "experiment": EXPERIMENT_TAG, "experiment_lv2": lv2_id, "taxonomy_filter_enabled": False,
    })

    def _on_progress(event) -> None:
        print(f"  [{event.processed}/{event.total}] {event.lv2_id}::{event.type_name} -> {event.outcome.status}")

    try:
        summary = run_collection(
            conn, providers, run_configs, run_id, targets,
            target_count=target_count, candidate_multiplier=candidate_multiplier,
            date_range_by_lv2=date_range_by_lv2, provider_ratio_by_lv2=provider_ratio_by_lv2,
            openai_client=openai_client, on_progress=_on_progress,
            use_adaptive_multiplier=use_adaptive_multiplier,
        )
    except Exception as error:  # noqa: BLE001 - 한 LV2 실패로 나머지 실험까지 멈추지 않게 한다
        runs_repo.finish_run(conn, run_id, "failed", {}, [f"실행 오류: {error}"])
        print(f"[fail] {lv2_id}: {error}")
        return None

    runs_repo.finish_run(conn, run_id, "completed", summary.provider_usage, summary.warnings)
    print(f"[done] {lv2_id}: run_id={run_id} accepted={summary.accepted}/{target_count}")
    return run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target-count", type=int, default=30)
    parser.add_argument("--candidate-multiplier", type=float, default=2.0)
    parser.add_argument("--lv2", action="append", help="특정 LV2 id만 실행 (반복 지정 가능). 생략하면 전체 LV2.")
    parser.add_argument("--confirm", action="store_true", help="실제 API 호출(비용 발생)에 동의")
    parser.add_argument(
        "--adaptive-multiplier", action="store_true",
        help="candidate_multiplier 대신 provider별 실측 생존율 기반 배수를 쓴다 (collection.yaml의 "
             "adaptive_multiplier 설정, 표본 min_samples 미만인 (lv2,type,provider)는 initial 고정값)",
    )
    args = parser.parse_args()

    if not args.confirm:
        print("실제 API를 호출해 비용이 발생합니다. 동의하면 --confirm을 붙여 다시 실행하세요.")
        return

    configs = load_all_configs()
    validate_configs(configs)
    db_path = PROJECT_ROOT / configs["app"]["database"]["path"]
    conn = connect(db_path)

    lv2_ids = args.lv2 or [g["lv2_id"] for g in taxonomy_groups(configs)]
    print(f"실험 대상 LV2 {len(lv2_ids)}개, target_count={args.target_count}")

    with LOG_PATH.open("a", encoding="utf-8") as log_f:
        for lv2_id in lv2_ids:
            run_id = run_one_lv2(
                conn, configs, lv2_id, target_count=args.target_count, candidate_multiplier=args.candidate_multiplier,
                use_adaptive_multiplier=args.adaptive_multiplier,
            )
            if run_id:
                log_f.write(json.dumps({
                    "lv2_id": lv2_id, "run_id": run_id, "target_count": args.target_count,
                    "logged_at": datetime.now(timezone.utc).isoformat(),
                }, ensure_ascii=False) + "\n")
                log_f.flush()


if __name__ == "__main__":
    main()
