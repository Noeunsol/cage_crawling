"""시스템/유저 프롬프트를 코드에서 분리해 prompts/*.yaml로 관리한다.

설계 원칙: 모든 LLM 시스템 프롬프트는 코드가 아니라 yaml에 둔다. rubric·예시·형식을
프롬프트 파일에서 설계하고, 코드는 값만 채워 렌더한다.

스키마
  필수: system_prompt, user_prompt
  옵션: condition(프리필/가짜 어시스턴트 응답), strategy_format, type_format,
        example_header, example_format, examples, required_inputs
required_inputs: 렌더 시 반드시 채워야 하는 변수를 field별로 선언한다(없으면 명확한 오류).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# configs/taxonomy.yaml의 llm_rubric 중 프롬프트로 내보낼 키와 라벨.
# few_shot_examples(67개분 25k자)는 일부러 뺐다 — 다 넣으면 taxonomy 블록이 6.5배가 되고,
# 예시는 prompts/taxonomy_mapping.yaml의 오분류 실사례 선별본이 이미 담당한다.
_RUBRIC_FIELDS = (
    ("prerequisite", "전제"),
    ("inclusion_criteria", "포함"),
    ("exclusion_criteria", "제외"),
    ("boundary_resolution", "경계"),
)


def render_rubric(subtype, indent: str = "        ") -> str:
    """type의 llm_rubric을 프롬프트 줄로 편다. rubric이 없으면 빈 문자열."""
    out = []
    for key, label in _RUBRIC_FIELDS:
        value = (getattr(subtype, "llm_rubric", None) or {}).get(key)
        if not value:
            continue
        for item in (value if isinstance(value, list) else [value]):
            out.append(f"{indent}[{label}] {str(item).strip()}")
    return ("\n".join(out) + "\n") if out else ""


@dataclass
class PromptSpec:
    system_prompt: str
    user_prompt: str
    condition: str = ""
    strategy_format: str = ""
    type_format: str = ""
    example_header: str = ""
    example_format: str = ""
    examples: list = field(default_factory=list)
    required_inputs: dict = field(default_factory=dict)
    path: str = ""

    @classmethod
    def load(cls, path: str) -> "PromptSpec":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for k in ("system_prompt", "user_prompt"):
            if not data.get(k):
                raise ValueError(f"{path}: 필수 프롬프트 필드 '{k}' 가 비었습니다.")
        allowed = {f for f in cls.__annotations__ if f != "path"}
        return cls(path=str(path), **{k: v for k, v in data.items() if k in allowed})

    # ── 렌더 ──
    def render_strategies(self, policies) -> str:
        """각 taxonomy(lv2)를 strategy_format으로, 내부 type을 type_format으로 조립.

        type 설명 아래에 configs/taxonomy.yaml의 llm_rubric(전제·포함·제외·경계)을 덧붙인다.
        이 블록은 호출마다 동일해 OpenAI 자동 prompt caching이 걸린다.
        """
        out = []
        for p in policies:
            types = "".join(
                self.type_format.format(
                    name=s.name, description=s.description or p.description or p.definition)
                + render_rubric(s)
                for s in p.subtypes
            )
            out.append(self.strategy_format.format(
                taxonomy_lv1=p.taxonomy_lv1, taxonomy_lv2=p.taxonomy_lv2,
                taxonomy_lv2_name=p.taxonomy_lv2_name or p.taxonomy_lv2,
                definition=p.definition, description=p.description, types=types))
        return "".join(out)

    def render_examples(self) -> str:
        if not self.examples:
            return ""
        return self.example_header + "".join(self.example_format.format(**e) for e in self.examples)

    def render_system(self, **kw) -> str:
        return self._render("system_prompt", self.system_prompt, kw)

    def render_user(self, **kw) -> str:
        return self._render("user_prompt", self.user_prompt, kw)

    def _render(self, name: str, template: str, kw: dict) -> str:
        missing = [v for v in self.required_inputs.get(name, []) if v not in kw]
        if missing:
            raise KeyError(f"{self.path}: '{name}' 렌더에 필요한 입력 {missing} 누락")
        try:
            return template.format(**kw)
        except KeyError as e:
            raise KeyError(f"{self.path}: '{name}' 템플릿의 {e} 미제공") from e


if __name__ == "__main__":
    import tempfile
    from dataclasses import dataclass as _dc

    @_dc
    class _St:
        name: str
        description: str

    @_dc
    class _Pol:
        taxonomy_lv1: str
        taxonomy_lv2: str
        taxonomy_lv2_name: str
        definition: str
        description: str
        subtypes: list

    y = """
required_inputs:
  system_prompt: [strategies]
  user_prompt: [title, body]
condition: ""
system_prompt: |
  분류기.
  {strategies}{examples}
user_prompt: |
  제목: {title}
  본문: {body}
strategy_format: |
  - {taxonomy_lv2} ({taxonomy_lv2_name}): {definition}
  {types}
type_format: "    - {name}: {description}\\n"
example_header: "예시:\\n"
example_format: "- {input} -> {label}\\n"
examples:
  - {input: "뉴스 보도", label: "fail"}
"""
    f = Path(tempfile.mktemp(suffix=".yaml"))
    f.write_text(y, encoding="utf-8")
    spec = PromptSpec.load(str(f))
    pols = [_Pol("L1", "1_A_Toxic", "Toxic", "정의A", "설명A", [_St("t1", "d1"), _St("t2", "")])]
    sysp = spec.render_system(strategies=spec.render_strategies(pols), examples=spec.render_examples())
    assert "1_A_Toxic (Toxic): 정의A" in sysp and "- t1: d1" in sysp
    assert "- t2: 설명A" in sysp, "type description 비면 policy description으로 fallback"
    assert "뉴스 보도 -> fail" in sysp
    usr = spec.render_user(title="제목x", body="본문y")
    assert "제목: 제목x" in usr and "본문: 본문y" in usr
    try:
        spec.render_user(title="only")           # body 누락 → 필수 입력 오류
        raise AssertionError("missing 입력이 통과됨")
    except KeyError as e:
        assert "body" in str(e), e
    print("prompt_loader self-check OK")
