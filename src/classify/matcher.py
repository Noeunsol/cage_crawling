"""Phase 10 — Taxonomy Matching.

두 경로:
- match()  : 키워드 모드(레거시). 주어진 (taxonomy_lv2, subtype)에 부합하는지 확인(confirm).
- classify(): 트렌드 모드. 공식 닫힌 어휘(19 lv2 × category) 중 argmax 1개로 단일 매핑.

공통: rule 먼저, 애매 구간만 LLM. LLM 입력은 masked_text만(raw 미전송). 프롬프트 injection 방어.
LLM provider는 config 선택(anthropic|openai). 파싱/검증 실패 시 rule fallback.
"""
from __future__ import annotations

import json
import logging

from ..policy import Policy, Subtype
from ..filtering.relevance_filter import SEVERITY_3, SEVERITY_4, SEVERITY_5
from ..filtering.risk_signals import detect_signals, primary_override
from ..llm_tracker import log_llm_usage
from ..schema import ContentRecord, MatchResult, content_id_for

log = logging.getLogger(__name__)

_SENSITIVE_HINTS = ("고소", "신고", "괴롭힘", "피해", "협박", "스토킹")
_AMBIGUITY_SIGNALS = ("영화", "드라마", "웹툰", "소설", "게임 리뷰", "노래", "앨범")

# confirm(match)용 스키마
_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "is_relevant": {"type": "boolean"},
        "subtype": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "safety_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["is_relevant", "subtype", "confidence", "reason", "safety_flags"],
    "additionalProperties": False,
}


# ── 공용 헬퍼 ──
def _text_of(rec: ContentRecord) -> str:
    comments = "\n".join(rec.masked_comments or [])
    return f"{rec.title}\n{rec.masked_text or rec.body_text}\n{comments}"


def _confidence(text: str, subtype: Subtype) -> tuple[float, int, int, int]:
    """subtype 키워드/패턴 적합도 → confidence(0~0.99) + hit 카운트."""
    kw = sum(1 for k in subtype.keywords if k in text)
    pos = sum(1 for p in subtype.positive_patterns if p in text)
    neg = sum(1 for n in subtype.negative_patterns if n in text)
    base = 0.4 if kw else 0.2
    c = base + 0.15 * min(kw, 3) + 0.1 * min(pos, 3) - 0.3 * neg
    return round(max(0.0, min(c, 0.99)), 3), kw, pos, neg


def _apply_side_scores(rec: ContentRecord, text: str, subtype: Subtype, confidence: float) -> None:
    """harmfulness/seed_source_value/taxonomy_relevance 등 부가 점수 기록(match/classify 공통)."""
    rec.taxonomy_relevance_score = confidence
    rec.taxonomy_fit_score = confidence
    harmful_hits = sum(1 for s in (subtype.target_harm_signals or subtype.keywords) if s in text)
    context_hits = sum(1 for s in subtype.positive_patterns if s in text)
    rec.harmfulness_score = round(min(0.2 + harmful_hits * 0.18 + context_hits * 0.12, 0.99), 3)
    locality = 1.0 if any("가" <= ch <= "힣" for ch in text) else 0.0
    specificity = min(len(text) / 1200, 1.0)
    expression = min((harmful_hits + context_hits) / 4, 1.0)
    rec.seed_source_value_score = round(max(0.0, 0.35 * locality + 0.35 * specificity
                                              + 0.3 * expression - 0.1 * (rec.pii_risk_score or 0)), 3)


def build_taxonomy_index(policies: list[Policy]) -> tuple[set[tuple[str, str]], list[str]]:
    """(lv2, type) 유효쌍 + LLM system rubric용 정본 설명."""
    valid: set[tuple[str, str]] = set()
    lines: list[str] = []
    for p in policies:
        cats = [s.name for s in p.subtypes]
        for c in cats:
            valid.add((p.taxonomy_lv2, c))
        label = f"{p.taxonomy_lv1} > {p.taxonomy_lv2}"
        if p.taxonomy_lv2_name:
            label += f" ({p.taxonomy_lv2_name})"
        lines.append(
            f"- {label}\n"
            f"  Definition: {p.definition}\n"
            f"  Description: {p.description}\n"
            "  types:\n" + "\n".join(
                f"    - {s.name}: {s.description or p.description or p.definition}"
                for s in p.subtypes
            )
        )
    return valid, lines


class TaxonomyMatcher:
    """교체 가능한 인터페이스."""
    def match(self, taxonomy_lv2: str, subtype: Subtype, rec: ContentRecord) -> MatchResult:
        raise NotImplementedError


class RuleBasedMatcher(TaxonomyMatcher):
    def __init__(self, review_threshold: float = 0.5):
        self.review_threshold = review_threshold

    def match(self, taxonomy_lv2, subtype, rec):
        text = _text_of(rec)
        confidence, kw_hits, pos_hits, neg_hits = _confidence(text, subtype)
        _apply_side_scores(rec, text, subtype, confidence)
        safety_flags = ["contains_sensitive_context"] if any(h in text for h in _SENSITIVE_HINTS) else []
        return MatchResult(
            is_relevant=confidence >= self.review_threshold,
            taxonomy_lv2=taxonomy_lv2, subtype=subtype.name,
            confidence=confidence,
            reason=f"rule: keyword={kw_hits}, positive={pos_hits}, negative={neg_hits}",
            safety_flags=safety_flags,
        )

    def classify(self, rec, policies) -> MatchResult:
        """공식 닫힌 어휘 전체에서 argmax 단일 매핑 + disambiguation override."""
        text = _text_of(rec)
        best_conf, best_lv2, best_st = -1.0, None, None
        for p in policies:
            for st in p.subtypes:
                conf, kw, _, _ = _confidence(text, st)
                rank = conf if kw else conf - 0.15   # 키워드 hit 있는 후보 우선
                if rank > best_conf:
                    best_conf, best_lv2, best_st = rank, p.taxonomy_lv2, st
        conf = _confidence(text, best_st)[0]

        # "욕설만으로 1_A 직행 금지". 단, 고정 우선순위가 아니라 **실제 키워드 증거**로 옮긴다:
        # 1_A가 argmax면 키워드가 잡히는 최선의 비-toxic taxonomy를 우선. (혐오 글의 과장된
        # "죽여" 같은 신호가 hate를 밀어내지 않도록.) 증거가 없을 때만 신호 우선순위 fallback.
        signals = detect_signals(text)
        if best_lv2 == "1_A_Toxic_Language":
            alternatives = [
                (_confidence(text, st)[0], p.taxonomy_lv2, st)
                for p in policies if p.taxonomy_lv2 != "1_A_Toxic_Language"
                for st in p.subtypes if _confidence(text, st)[1]
            ]
            alt_conf, alt_lv2, alt_st = max(alternatives, default=(-1.0, None, None))
            if alt_conf >= conf:
                best_lv2, best_st, conf = alt_lv2, alt_st, alt_conf
            else:
                ov = primary_override(signals)
                pol = next((p for p in policies if p.taxonomy_lv2 == ov), None) if ov else None
                if pol:
                    best_lv2 = ov
                    best_st = max(pol.subtypes, key=lambda s: _confidence(text, s)[0])
                    conf = max(conf, _confidence(text, best_st)[0])

        _apply_side_scores(rec, text, best_st, conf)
        safety_flags = ["contains_sensitive_context"] if any(h in text for h in _SENSITIVE_HINTS) else []
        matched = [k for k in best_st.keywords if k in text]
        return MatchResult(
            is_relevant=conf >= self.review_threshold,
            taxonomy_lv2=best_lv2, subtype=best_st.name, confidence=conf,
            reason=f"rule_classify: matched={matched}, signals={sorted(signals)}",
            safety_flags=safety_flags, matched_keywords=matched, source="rule",
        )


class LLMMatcher(TaxonomyMatcher):
    """LLM 분류기. provider=openai(gpt-4o-mini). 실패/검증오류 시 None(→rule fallback)."""
    def __init__(self, model: str = "gpt-4o-mini", max_chars: int = 4000,
                 provider: str = "openai", pricing: dict | None = None,
                 prompt_path: str = "prompts/taxonomy_mapping.yaml",
                 confirm_prompt_path: str = "prompts/taxonomy_confirm.yaml"):
        self.model = model
        self.max_chars = max_chars
        self.provider = provider
        self.pricing = pricing or {}
        self.prompt_path = prompt_path
        self.confirm_prompt_path = confirm_prompt_path
        self.last_usage = {"input": 0, "cached": 0, "output": 0, "total": 0}
        self.last_error = ""
        self._client = None
        self._specs: dict = {}

    def _spec(self, path: str):
        """prompts/*.yaml 시스템·유저 프롬프트 정의. path별 최초 1회 로드 후 캐시."""
        if path not in self._specs:
            from ..prompt_loader import PromptSpec
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

    def _complete_json(self, system: str, user: str, schema: dict, condition: str = "") -> dict | None:
        self.last_usage = {"input": 0, "cached": 0, "output": 0, "total": 0}
        self.last_error = ""
        try:
            if self.provider == "openai":
                messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
                if condition:  # 프리필/가짜 어시스턴트 응답
                    messages.append({"role": "assistant", "content": condition})
                resp = self._get_client().chat.completions.create(
                    model=self.model, max_tokens=512, temperature=0,  # 분류는 결정론적으로(재현성)
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
            a_messages = [{"role": "user", "content": user}]
            if condition:  # 프리필/가짜 어시스턴트 응답
                a_messages.append({"role": "assistant", "content": condition})
            resp = self._get_client().messages.create(
                model=self.model, max_tokens=512, system=system,
                messages=a_messages,
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            text = next(b.text for b in resp.content if b.type == "text")
            usage = getattr(resp, "usage", None)
            self.last_usage = {
                "input": int(getattr(usage, "input_tokens", 0) or 0),
                "cached": 0,
                "output": int(getattr(usage, "output_tokens", 0) or 0),
                "total": int(getattr(usage, "input_tokens", 0) or 0)
                         + int(getattr(usage, "output_tokens", 0) or 0),
            }
            return json.loads(text)
        except Exception as e:  # noqa: BLE001 (API/파싱 오류 → rule fallback)
            self.last_error = f"llm_api_or_json_error:{type(e).__name__}"
            log.info("LLM 호출 실패(%s) → rule fallback", e)
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
        body = (rec.masked_text or rec.body_text)[: self.max_chars]
        return f"제목: {rec.title}\n본문:\n{body}"

    def match(self, taxonomy_lv2, subtype, rec) -> MatchResult | None:
        spec = self._spec(self.confirm_prompt_path)
        system = spec.render_system(
            taxonomy_lv2=taxonomy_lv2, subtype_name=subtype.name,
            subtype_description=subtype.description or "",
        )
        user = spec.render_user(
            title=rec.title, body=(rec.masked_text or rec.body_text)[: self.max_chars])
        data = self._complete_json(system, user, _LLM_SCHEMA, spec.condition)
        if data is None:
            return None
        self._record_usage(rec, self.confirm_prompt_path)
        conf = round(float(data.get("confidence", 0.0)), 3)
        rec.taxonomy_relevance_score = conf
        rec.taxonomy_fit_score = conf
        return MatchResult(
            is_relevant=bool(data.get("is_relevant", False)), taxonomy_lv2=taxonomy_lv2,
            subtype=data.get("subtype") or subtype.name, confidence=conf,
            reason=f"llm: {data.get('reason', '')}", safety_flags=list(data.get("safety_flags", [])),
        )

    def classify(self, rec, policies, valid_pairs=None, taxo_lines=None) -> MatchResult | None:
        """관련성·19종 taxonomy·유해성·한국 맥락을 한 번에 판정한다."""
        canonical_pairs, canonical_lines = build_taxonomy_index(policies)
        valid_pairs = valid_pairs or canonical_pairs
        taxo_lines = taxo_lines or canonical_lines
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
        )
        user = spec.render_user(
            title=rec.title,
            body=(rec.masked_text or rec.body_text)[: self.max_chars],
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


class TieredMatcher(TaxonomyMatcher):
    """rule 먼저 → 애매/ambiguity에서만 LLM. LLM off거나 실패면 rule 결과."""
    def __init__(self, rule: RuleBasedMatcher, llm: LLMMatcher | None,
                 auto_save: float, review_th: float):
        self.rule = rule
        self.llm = llm
        self.auto_save = auto_save
        self.review_th = review_th

    def match(self, taxonomy_lv2, subtype, rec):
        result = self.rule.match(taxonomy_lv2, subtype, rec)
        reason = self._escalate_reason(result, rec)
        if not reason or self.llm is None:
            return result
        llm_result = self.llm.match(taxonomy_lv2, subtype, rec)
        if llm_result is None:
            rec.llm_escalation_reason = f"{reason}; llm_parse_error->rule_fallback"
            rec.taxonomy_relevance_score = result.confidence
            return result
        rec.llm_escalation_reason = reason
        return llm_result

    def classify(self, rec, policies, valid_pairs=None, taxo_lines=None):
        result = self.rule.classify(rec, policies)
        reason = self._escalate_reason(result, rec)
        if not reason or self.llm is None:
            return result
        if valid_pairs is None:
            valid_pairs, taxo_lines = build_taxonomy_index(policies)
        llm_result = self.llm.classify(rec, policies, valid_pairs, taxo_lines)
        if llm_result is None:
            rec.llm_escalation_reason = f"{reason}; llm_parse_error->rule_fallback"
            rec.taxonomy_relevance_score = result.confidence
            return result
        rec.llm_escalation_reason = reason
        return llm_result

    def _escalate_reason(self, result: MatchResult, rec: ContentRecord) -> str | None:
        text = _text_of(rec)
        if self.review_th <= result.confidence < self.auto_save:
            return "ambiguous_band"
        if any(s in text for s in _AMBIGUITY_SIGNALS):
            return "media_review_ambiguity"
        if rec.value_score >= 0.8 and result.confidence < self.auto_save:
            return "high_value_low_confidence"
        return None


# ── 트렌드 모드 스코어링 (1~5, 규칙) ── gate(relevance_filter) 어휘와 일치하는 등급 세트 사용
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
