import asyncio
import csv
from types import SimpleNamespace

from experiments.generate_llm_only_baseline import allocate_counts, generate_type, write_csv
from src.utils.prompts import load_prompt, render_prompt


def test_round_robin_allocation():
    assert allocate_counts(30, 5) == [6, 6, 6, 6, 6]
    assert allocate_counts(30, 4) == [8, 8, 7, 7]


def test_generation_prompt_renders_all_taxonomy_inputs():
    prompt = load_prompt("llm_only_context_generation")
    system, user = render_prompt(
        prompt, lv2_category="category", type_name="type", definition="definition",
        include_criteria="- include", exclude_criteria="- exclude",
        target_region="South Korea", count="3",
    )
    assert "외부 도구" in system
    assert "category" in user
    assert "정확히 3개" in user


def test_generate_type_retries_after_exact_duplicate():
    calls = 0

    async def fake_call(*args, **kwargs):
        nonlocal calls
        calls += 1
        contexts = [
            {"title": "라벨", "content": "설명입니다. 두 번째 문장입니다.", "context_kind": "generalized_pattern"},
            {"title": f"라벨 {calls}", "content": f"설명 {calls}입니다. 두 번째 문장입니다.", "context_kind": "hypothetical_scenario"},
        ]
        return SimpleNamespace(
            data={"contexts": contexts}, prompt_tokens=10, completion_tokens=5, elapsed_s=0.1,
        )

    lv2 = {"lv2_name": "category"}
    type_cfg = {"name": "type", "definition": "definition", "include_criteria": [], "exclude_criteria": []}
    rows, usage = asyncio.run(generate_type(
        None, {"version": 1}, lv2, type_cfg, 3,
        batch_size=3, max_retries=3, seen=set(), call=fake_call,
    ))

    assert len(rows) == 3
    assert calls == 2
    assert usage["prompt_tokens"] == 20


def test_csv_matches_evaluator_input_format(tmp_path):
    path = tmp_path / "LV2" / "type.csv"
    write_csv(path, [{
        "title": "라벨", "content": "독립적인 맥락 설명입니다.", "context_kind": "generalized_pattern",
    }], "LV2", "type", 1)

    with path.open(encoding="utf-8-sig", newline="") as file:
        row = next(csv.DictReader(file))
    assert row["title"] == "라벨"
    assert row["content"] == "독립적인 맥락 설명입니다."
    assert row["context_kind"] == "generalized_pattern"
