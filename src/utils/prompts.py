"""prompts/*.yaml 로더. 모든 OpenAI system/user prompt는 코드에 하드코딩하지 않고 여기서 읽는다 (2.2절)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
from openai import OpenAI

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


@dataclass
class StructuredOutputResult:
    data: dict
    prompt_tokens: int
    completion_tokens: int
    elapsed_s: float


def load_prompt(name: str) -> dict:
    path = PROMPTS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"프롬프트 파일이 없습니다: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def render_prompt(prompt_cfg: dict, **inputs: str) -> tuple[str, str]:
    """required_inputs가 다 채워졌는지 확인하고 (system_prompt, user_prompt)를 렌더링해서 돌려준다."""
    missing = [key for key in prompt_cfg["required_inputs"] if key not in inputs]
    if missing:
        raise ValueError(
            f"프롬프트 '{prompt_cfg['name']}' 렌더링에 필요한 입력이 없습니다: {missing}"
        )
    system_prompt = prompt_cfg["system_prompt"].format(**inputs)
    user_prompt = prompt_cfg["user_prompt"].format(**inputs)
    return system_prompt, user_prompt


def call_structured_output(client: OpenAI, prompt_cfg: dict, model: str, **inputs: str) -> StructuredOutputResult:
    """prompt를 렌더링해서 호출하고, output_schema에 맞는 JSON과 토큰/시간 사용량을 함께 돌려준다."""
    system_prompt, user_prompt = render_prompt(prompt_cfg, **inputs)
    started = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": prompt_cfg["name"], "schema": prompt_cfg["output_schema"], "strict": True},
        },
    )
    elapsed_s = time.monotonic() - started
    return StructuredOutputResult(
        data=json.loads(response.choices[0].message.content),
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        elapsed_s=elapsed_s,
    )
