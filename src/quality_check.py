"""내보낸 콘텐츠 CSV를 기존 quality_score 프롬프트로 채점한다."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from openai import OpenAI

from src.utils.prompts import call_structured_output, load_prompt


def find_type_definition(configs: dict, lv2_id: str, type_name: str) -> str:
    fallback = []
    for group in configs["taxonomy"]["taxonomy"]:
        for type_cfg in group["types"]:
            if type_cfg["name"] == type_name:
                fallback.append(type_cfg["definition"])
                if group["lv2_id"] == lv2_id:
                    return type_cfg["definition"]
    if len(fallback) == 1:
        return fallback[0]
    raise ValueError(f"Taxonomy type을 찾을 수 없습니다: {lv2_id}/{type_name}")


def score_csv(path: Path, configs: dict, limit: int, on_progress=None) -> list[dict]:
    lv2_id, type_name = path.parent.name, path.stem.removeprefix("fake_")
    definition = find_type_definition(configs, lv2_id, type_name)
    prompt = load_prompt("quality_score")
    model = configs["providers"]["openai"]["model"]
    client = OpenAI()

    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))[:limit]

    scored = []
    for index, row in enumerate(rows, 1):
        result = call_structured_output(
            client, prompt, model,
            title=row["title"], content=row["content"], type_name=type_name, definition=definition,
        )
        data = result.data
        scored.append({
            **row,
            "specificity": data["specificity"],
            "content_quality": data["content_quality"],
            "relevance_strength": data["relevance_strength"],
            "korean_locality": data["korean_locality"],
            "overall": round(sum(data[k] for k in (
                "specificity", "content_quality", "relevance_strength", "korean_locality",
            )) / 4, 2),
            "quality_reason": data["reason"],
            "quality_issues": json.dumps(data["issues"], ensure_ascii=False),
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
        })
        if on_progress:
            on_progress(index, len(rows))
    return scored


def write_scores(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)


def load_db_scores(path: Path, db_path: Path) -> list[dict]:
    """CSV의 URL과 일치하는 기존 DB 품질 점수를 CSV 순서대로 반환한다."""
    lv2_id, type_name = path.parent.name, path.stem.removeprefix("fake_")
    with path.open(encoding="utf-8-sig", newline="") as f:
        source_rows = list(csv.DictReader(f))

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    scored = conn.execute(
        """
        SELECT c.canonical_url AS url, qs.specificity, qs.content_quality,
               qs.relevance_strength, qs.korean_locality, qs.overall,
               qs.reason AS quality_reason, qs.issues AS quality_issues,
               qs.prompt_tokens, qs.completion_tokens, qs.scored_at
        FROM content_quality_scores qs
        JOIN contents c ON c.id = qs.content_id
        WHERE qs.taxonomy_lv2 = ? AND qs.type_name = ?
        ORDER BY qs.scored_at DESC
        """,
        (lv2_id, type_name),
    ).fetchall()
    conn.close()
    by_url = {}
    for row in scored:
        by_url.setdefault(row["url"], dict(row))
    return [{**row, **by_url[row["url"]]} for row in source_rows if row.get("url") in by_url]
