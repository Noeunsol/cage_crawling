"""Taxonomy 분류 — 공식 닫힌 어휘(19 lv2 x type) 중 1개로 단일 매핑.

classify()가 유일한 경로다. 1차 trend는 본문을 보고 taxonomy를 정하고,
2차는 acceptance gate가 저장을 판정하되 사후 재검수에서만 이 분류기를 쓴다.
LLM 입력은 정제 본문만(raw 미전송)이고 프롬프트 injection 방어 문구가 들어간다.
provider는 openai 고정. 파싱/검증 실패 시 None -> 호출부가 fail-closed 처리.
"""
from __future__ import annotations

import json
import logging

from src.common.policy import Policy
from src.common.filtering.risk_signals import SEVERITY_3, SEVERITY_4, SEVERITY_5

from src.common.llm_tracker import log_llm_usage
from src.common.schema import ContentRecord, MatchResult, content_id_for

log = logging.getLogger(__name__)

# ── 공용 헬퍼 ──
def _text_of(rec: ContentRecord) -> str:
    return f"{rec.title}\n{rec.core_text or rec.body_text}"


def build_taxonomy_index(policies: list[Policy]) -> set[tuple[str, str]]:
    """모델이 고를 수 있는 (lv2, type) 유효쌍. 응답 검증에 쓴다.

    시스템 프롬프트 문안은 여기서 만들지 않는다 — prompts/taxonomy_mapping.yaml의
    strategy_format/type_format을 PromptSpec.render_strategies가 렌더한다.
    """
    return {(p.taxonomy_lv2, s.name) for p in policies for s in p.subtypes}


class LLMMatcher:
    """LLM 분류기. provider=openai(gpt-4o-mini). 실패/검증오류 시 None(→호출부가 fail-closed)."""
    def __init__(self, model: str = "gpt-4o-mini", max_chars: int = 4000,
                 provider: str = "openai", pricing: dict | None = None,
                 prompt_path: str = "prompts/taxonomy_mapping.yaml"):
        self.model = model
        self.max_chars = max_chars
        self.provider = provider
        self.pricing = pricing or {}
        self.prompt_path = prompt_path
        self.last_usage = {"input": 0, "cached": 0, "output": 0, "total": 0}
        self.last_error = ""
        self._client = None
        self._specs: dict = {}

    def _spec(self, path: str):
        """prompts/*.yaml 시스템·유저 프롬프트 정의. path별 최초 1회 로드 후 캐시."""
        if path not in self._specs:
            from src.common.prompt_loader import PromptSpec
            self._specs[path] = PromptSpec.load(path)
        return self._specs[path]

    def _get_client(self):
        if self._client is None:
            from dotenv import load_dotenv
            load_dotenv()  # 프로젝트 .env; 기존 환경변수는 덮어쓰지 않는다.
            if self.provider != "openai":
                raise ValueError(f"unsupported LLM provider: {self.provider}")
            from openai import OpenAI   # lazy: 필요할 때만 dep
            self._client = OpenAI()
        return self._client

    def _complete_json(self, system: str, user: str, schema: dict, condition: str = "",
                       max_tokens: int = 512) -> dict | None:
        self.last_usage = {"input": 0, "cached": 0, "output": 0, "total": 0}
        self.last_error = ""
        try:
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            if condition:  # 프리필/가짜 어시스턴트 응답
                messages.append({"role": "assistant", "content": condition})
            resp = self._get_client().chat.completions.create(
                model=self.model, max_tokens=max_tokens, temperature=0,  # 분류는 결정론적으로(재현성)
                messages=messages,
                response_format={"type": "json_schema",
                                 "json_schema": {"name": "cls", "strict": True, "schema": schema}},
            )
            usage = resp.usage
            details = getattr(usage, "prompt_tokens_details", None)
            self.last_usage = {
                "input": int(getattr(usage, "prompt_tokens", 0) or 0),
                "cached": int(getattr(details, "cached_tokens", 0) or 0),
                "output": int(getattr(usage, "completion_tokens", 0) or 0),
                "total": int(getattr(usage, "total_tokens", 0) or 0),
            }
            return json.loads(resp.choices[0].message.content)
        except Exception as e:  # noqa: BLE001 (API/파싱 오류 → fail-closed)
            self.last_error = f"llm_api_or_json_error:{type(e).__name__}"
            log.info("LLM 호출 실패(%s) → 분류 없음", e)
            return None

    def _record_usage(self, rec, prompt_file: str = "") -> None:
        usage = self.last_usage
        rec.llm_model = self.model
        rec.llm_input_tokens = usage["input"]
        rec.llm_cached_input_tokens = usage["cached"]
        rec.llm_output_tokens = usage["output"]
        rec.llm_total_tokens = usage["total"] or usage["input"] + usage["output"]
        uncached = max(0, usage["input"] - usage["cached"])
        cost = (
            uncached * float(self.pricing.get("input_per_million_usd", 0))
            + usage["cached"] * float(self.pricing.get("cached_input_per_million_usd", 0))
            + usage["output"] * float(self.pricing.get("output_per_million_usd", 0))
        ) / 1_000_000
        rec.llm_estimated_cost_usd = round(cost, 8)
        log_llm_usage(
            run_id=getattr(rec, "run_id", "") or "",
            content_id=getattr(rec, "content_id", "") or content_id_for(getattr(rec, "source_url", "")),
            provider=self.provider, model=self.model, prompt_file=prompt_file or self.prompt_path,
            input=usage["input"], cached=usage["cached"], output=usage["output"],
            total=rec.llm_total_tokens, cost_usd=rec.llm_estimated_cost_usd,
        )

    def _user_text(self, rec) -> str:
        body = (rec.core_text or rec.body_text)[: self.max_chars]
        return f"제목: {rec.title}\n본문:\n{body}"

    def classify(self, rec, policies, valid_pairs=None,
                 broad_candidate: bool = False) -> MatchResult | None:
        """관련성·19종 taxonomy·유해성·한국 맥락을 한 번에 판정한다."""
        valid_pairs = valid_pairs or build_taxonomy_index(policies)
        path_by_type = {
            st.name: (p.taxonomy_lv1, p.taxonomy_lv2)
            for p in policies for st in p.subtypes
        }
        path_by_lv2 = {p.taxonomy_lv2: p.taxonomy_lv1 for p in policies}
        schema = {
            "type": "object",
            "properties": {
                "is_taxonomy_relevant": {"type": "boolean"},
                "filter_status": {"type": "string", "enum": ["pass", "review", "fail"]},
                "category": {"enum": [None, "-", *sorted(path_by_type)]},
                "taxonomy_lv2": {"enum": [None, *sorted(path_by_lv2)]},
                "is_harmful": {"type": "boolean"},
                "harmfulness_score": {"type": "number"},
                "contains_korean_context": {"type": "boolean"},
                "korea_relevance_score": {"type": "number"},
                "concrete_context_score": {"type": "number"},
                "confidence": {"type": "number"},
                "reason": {"type": "string"},
                "evidence_spans": {"type": "array", "items": {"type": "string"}},
                "fail_reason": {"type": ["string", "null"]},
            },
            "required": [
                "is_taxonomy_relevant", "filter_status", "category", "taxonomy_lv2", "is_harmful",
                "harmfulness_score", "contains_korean_context", "korea_relevance_score",
                "concrete_context_score", "confidence", "reason", "evidence_spans",
                "fail_reason",
            ],
            "additionalProperties": False,
        }
        spec = self._spec(self.prompt_path)
        system = spec.render_system(
            strategies=spec.render_strategies(policies),
            examples=spec.render_examples(),
            collection_policy=(
                "2차 broad candidate 모드: 한국 관련 맥락이 있고 유해한 표현·행동·조장성이 있으며 "
                "한 taxonomy와 의미상 연결되면, 전형적인 방법 안내가 아니어도 relevant=true, "
                "filter_status=review로 판정하라. 단순 예방 안내·윤리강령·통계처럼 유해 요소 자체가 없는 "
                "문서는 relevant=false다."
                if broad_candidate else
                "일반 모드: taxonomy 정의의 포함·제외 기준을 엄격히 적용하라."
            ),
        )
        user = spec.render_user(
            title=rec.title,
            body=(rec.core_text or rec.body_text)[: self.max_chars],
        )
        data = self._complete_json(system, user, schema, spec.condition)
        if data is None:
            return None
        self._record_usage(rec)
        cat = data.get("category")
        lv2_name = data.get("taxonomy_lv2")
        relevant = bool(data.get("is_taxonomy_relevant"))
        if relevant and not cat and not lv2_name:
            self.last_error = "llm_validation_error:relevant_without_type"
            log.info("LLM 관련 응답에 type이 없음")
            return None
        if cat and cat != "-" and cat not in path_by_type:  # schema enum의 이중 방어
            self.last_error = "llm_validation_error:unknown_type"
            return None
        if relevant and cat == "-" and lv2_name not in path_by_lv2:
            self.last_error = "llm_validation_error:relevant_without_lv2"
            return None
        if not relevant and (cat or lv2_name):
            # 관련 없음과 category가 모순되면 fail-closed discard가 되도록 관련성 판정을 존중한다.
            data["filter_status"] = "fail"
        if cat and cat != "-" and cat in path_by_type:
            lv1, lv2 = path_by_type[cat]
        elif lv2_name in path_by_lv2:
            lv1, lv2 = path_by_lv2[lv2_name], lv2_name
        else:
            lv1, lv2 = (None, None)
        conf = round(float(data.get("confidence", 0.0)), 3)
        rec.harmfulness_score = round(max(0.0, min(float(data.get("harmfulness_score", 0)), 1.0)), 3)
        rec.is_harmful = bool(data.get("is_harmful"))
        rec.concrete_context_score = round(
            max(0.0, min(float(data.get("concrete_context_score", 0)), 1.0)), 3
        )
        rec.contains_korean_context = bool(data.get("contains_korean_context"))
        rec.korea_relevance_score = round(
            max(0.0, min(float(data.get("korea_relevance_score", 0)), 1.0)), 3
        )
        rec.taxonomy_relevance_score = conf
        rec.taxonomy_fit_score = conf
        rec.evidence_spans = list(data.get("evidence_spans", []))[:5]
        rec.filter_status = data.get("filter_status", "review")
        return MatchResult(is_relevant=relevant, taxonomy_lv2=lv2 or "", subtype=cat or "-", confidence=conf,
                           taxonomy_lv1=lv1 or "", reason=f"llm_classify: {data.get('reason', '')}",
                           evidence_spans=rec.evidence_spans,
                           safety_flags=[], source="llm")


def risk_score_of(signals: set[str], harmfulness: float | None) -> int:
    if signals & SEVERITY_5:
        return 5
    if signals & SEVERITY_4:
        return 4
    if signals & SEVERITY_3:
        return 3 if (harmfulness or 0) >= 0.6 else 2
    return 1


def trend_score_of(days_old: int | None, comment_count: int | None, is_trending: bool) -> int:
    c = comment_count or 0
    recent3 = days_old is not None and days_old <= 3
    recent7 = days_old is not None and days_old <= 7
    if (is_trending or c >= 100) and recent3:
        return 5
    if c >= 30 and recent7:
        return 4
    if recent3:
        return 3
    if recent7:
        return 2
    return 1


def confidence_bucket(conf01: float) -> int:
    for th, val in ((0.9, 5), (0.75, 4), (0.5, 3), (0.25, 2)):
        if conf01 >= th:
            return val
    return 1


# ── 분류기 조립 (1차 trend · 2차 재검수 공용) ──

def load_llm_config(settings: dict) -> dict:
    """configs/llm.yaml(모델/가격) + 호출부 settings.matching.llm 병합. settings가 우선."""
    import yaml
    from src.common import paths
    file_cfg = {}
    try:
        with open(paths.LLM_CONFIG, encoding="utf-8") as f:
            file_cfg = (yaml.safe_load(f) or {}).get("llm", {}) or {}
    except FileNotFoundError:
        pass
    return {**file_cfg, **(settings.get("matching", {}).get("llm", {}) or {})}


def build_llm(settings: dict) -> "LLMMatcher | None":
    """configs/llm.yaml 기준으로 분류기를 만든다. enabled=false면 None(호출부가 fail-closed)."""
    llm_cfg = load_llm_config(settings)
    if not llm_cfg.get("enabled"):
        return None
    provider = llm_cfg.get("provider", "openai")
    if provider != "openai":
        raise ValueError(f"unsupported LLM provider: {provider}")
    return LLMMatcher(model=llm_cfg.get("model", "gpt-4o-mini"),
                      max_chars=llm_cfg.get("max_chars", 4000),
                      provider=provider,
                      pricing=llm_cfg.get("pricing", {}),
                      prompt_path=llm_cfg.get("prompt_path", "prompts/taxonomy_mapping.yaml"))


def copy_llm_usage(rec, cand) -> None:
    """discard 콘텐츠까지 전체 사용량을 집계할 수 있도록 후보에도 usage를 기록한다."""
    for name in (
        "llm_model", "llm_input_tokens", "llm_cached_input_tokens",
        "llm_output_tokens", "llm_total_tokens", "llm_estimated_cost_usd",
    ):
        setattr(cand, name, getattr(rec, name, 0))
