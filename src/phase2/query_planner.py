"""2차 targeted 검색 계획 생성 — OpenAI를 쓰는 유일한 지점.

책임은 하나다: LV2·Type·최근 seed를 보고 '어디서 무엇을 검색할지'를 만든다.
본문 분류는 하지 않는다(그건 acceptance.evaluate의 몫).

LLM 출력은 그대로 실행하지 않는다.
  - validate_query_plan()이 미등록 type/source, 빈 evidence를 걸러낸다
  - forbidden_intents는 프롬프트에 더해 문자열 규칙으로 한 번 더 차단한다
    (안전 규칙 준수를 모델에 맡기지 않는다)
  - type별 query_budget / max_queries_per_lv2로 절단한다

계획은 매 실행마다 새로 만들지 않는다. should_regenerate() 참고.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

from ..prompt_loader import PromptSpec
from .intent_builder import _HIGH_RISK_TERMS

log = logging.getLogger(__name__)

# forbidden_intent 이름 → 실제 한국어 검색어에 나타나는 표현.
# LLM이 규칙을 지켰다고 주장해도 여기서 다시 검사한다.
_FORBIDDEN_TERMS = {
    "manufacturing": ("제조", "만드는 법", "만드는법", "제작법", "생산 방법"),
    "synthesis": ("합성", "합성법", "정제법"),
    "procurement": ("조달", "구매", "구입처", "판매처", "거래처", "텔레그램"),
    "optimization": ("수율", "위력 극대화", "효율 극대화", "개량"),
    "weaponization": ("무기화", "살상력", "폭탄 제작"),
    "how_to": ("하는 법", "하는법", "방법 정리", "튜토리얼"),
    "doxxing_howto": ("신상 터는 법", "신상털이 방법", "추적하는 법"),
    "overseas_only": (),      # 해외 전용 의도는 아래 korea evidence 요구로 걸러진다
    "general_definition": ("정의", "란 무엇", "개념 정리", "위키"),
    "material_sharing": ("다운로드", "토렌트", "공유 링크"),
    "explicit_media": ("무삭제", "원본 영상"),
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "target_type": {"type": "string"},
                    "query_kind": {"type": "string"},
                    "source_id": {"type": "string"},
                    "expected_korea_evidence": {"type": "array", "items": {"type": "string"}},
                    "expected_lv2_evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query", "target_type", "query_kind", "source_id",
                             "expected_korea_evidence", "expected_lv2_evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["queries"],
    "additionalProperties": False,
}


@dataclass
class QueryPlan:
    query: str
    target_lv2: str
    target_type: str
    query_kind: str
    source_id: str
    expected_korea_evidence: list[str] = field(default_factory=list)
    expected_lv2_evidence: list[str] = field(default_factory=list)
    generation_source: str = "openai_planner"   # openai_planner | config_fallback | cached_plan
    seed_url: str = ""

    @property
    def plan_id(self) -> str:
        basis = f"{self.target_lv2}|{self.target_type}|{self.source_id}|{self.query}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]

    def to_row(self, seed_fingerprint: str, created_at: str) -> dict:
        return {
            "plan_id": self.plan_id, "lv2": self.target_lv2, "target_type": self.target_type,
            "query": self.query, "query_kind": self.query_kind, "source_id": self.source_id,
            "expected_korea_evidence": self.expected_korea_evidence,
            "expected_lv2_evidence": self.expected_lv2_evidence,
            "seed_fingerprint": seed_fingerprint, "generation_source": self.generation_source,
            "created_at": created_at, "seed_url": self.seed_url,
        }


@lru_cache(maxsize=4)
def _plan_spec(path: str = "prompts/query_plan.yaml") -> PromptSpec:
    return PromptSpec.load(path)


def _searchable_sources(strategy: dict) -> list[dict]:
    """blocked는 호출하지 않는다. board_list는 검색어를 쓰지 않으므로 계획 대상이 아니다."""
    return [
        s for s in sorted(strategy.get("sources") or [], key=lambda s: s.get("priority", 99))
        if s.get("access") != "blocked" and s.get("method") != "board_list"
    ]


def contains_forbidden_intent(query: str, strategy: dict) -> bool:
    text = query.lower()
    if any(term in text for term in _HIGH_RISK_TERMS):
        return True
    for name in (strategy.get("planner") or {}).get("forbidden_intents") or []:
        if any(term in query for term in _FORBIDDEN_TERMS.get(name, ())):
            return True
    return False


def validate_query_plan(plan: QueryPlan, strategy: dict) -> bool:
    if not plan.query.strip():
        return False
    if plan.target_type not in (strategy.get("target_types") or {}):
        return False
    if plan.source_id not in {s["id"] for s in _searchable_sources(strategy)}:
        return False
    if contains_forbidden_intent(plan.query, strategy):
        log.warning("query plan blocked by forbidden intent: %s", plan.query)
        return False
    return bool(plan.expected_korea_evidence and plan.expected_lv2_evidence)


def apply_budgets(plans: list[QueryPlan], strategy: dict, max_total: int) -> list[QueryPlan]:
    """type별 query_budget = 검색 호출 예산. 저장 목표량(lv2_store_target)과 다른 축이다."""
    budgets = {name: (cfg or {}).get("query_budget", 0)
               for name, cfg in (strategy.get("target_types") or {}).items()}
    used: dict[str, int] = {}
    kept: list[QueryPlan] = []
    for plan in plans:
        if used.get(plan.target_type, 0) >= budgets.get(plan.target_type, 0):
            continue
        used[plan.target_type] = used.get(plan.target_type, 0) + 1
        kept.append(plan)
        if len(kept) >= max_total:
            break
    return kept


def seed_fingerprint(seeds: list[dict]) -> str:
    basis = "|".join(sorted(str(seed.get("source_url", "")) for seed in seeds))
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def latest_generation(rows: list[dict]) -> list[dict]:
    """가장 최근에 생성된 계획 세대만 남긴다. 옛 세대가 섞이면 재사용 판단이 뒤집힌다."""
    if not rows:
        return []
    newest = max((row.get("created_at") or "") for row in rows)
    return [row for row in rows if (row.get("created_at") or "") == newest]


def should_regenerate(cached: list[dict], seeds: list[dict], cfg: dict, today: date) -> bool:
    """계획 재생성 조건: seed 변경 / 오래됨 / 성과 저조. 그 외에는 기존 계획을 그대로 쓴다."""
    if not cached:
        return True
    reuse = cfg.get("reuse") or {}
    if seed_fingerprint(seeds) != (cached[0].get("seed_fingerprint") or ""):
        return True
    for row in cached:
        try:
            age = (today - date.fromisoformat((row.get("created_at") or "")[:10])).days
        except ValueError:
            return True
        if age >= int(reuse.get("max_age_days", 7)):   # "7일 이상 지나면 재생성"
            return True
    stored = sum(int(row.get("stored_count") or 0) for row in cached)
    return stored / len(cached) < float(reuse.get("min_stored_per_query", 1))


def plans_from_rows(rows: list[dict]) -> list[QueryPlan]:
    return [
        QueryPlan(
            query=row["query"], target_lv2=row["lv2"], target_type=row["target_type"],
            query_kind=row.get("query_kind", ""), source_id=row["source_id"],
            expected_korea_evidence=row.get("expected_korea_evidence") or [],
            expected_lv2_evidence=row.get("expected_lv2_evidence") or [],
            generation_source="cached_plan", seed_url=row.get("seed_url") or "",
        )
        for row in rows
    ]


def low_performing_queries(rows: list[dict], limit: int = 5) -> list[str]:
    return [row["query"] for row in rows if int(row.get("stored_count") or 0) == 0][:limit]


def fallback_plans(lv2: str, strategy: dict, queries_by_type: dict,
                   include_by_type: dict | None = None) -> list[QueryPlan]:
    """LLM 실패 시 기존 collection_intents_by_lv2 검색어를 계획으로 승격한다."""
    sources = _searchable_sources(strategy)
    if not sources:
        return []
    include_by_type = include_by_type or {}
    plans = [
        QueryPlan(
            query=query, target_lv2=lv2, target_type=target_type, query_kind="event",
            source_id=sources[0]["id"],
            expected_korea_evidence=["한국", "국내"],
            expected_lv2_evidence=include_by_type.get(target_type) or [target_type],
            generation_source="config_fallback",
        )
        for target_type in (strategy.get("target_types") or {})
        for query in queries_by_type.get(target_type, [])
    ]
    return [p for p in plans if validate_query_plan(p, strategy)]


class QueryPlanner:
    def __init__(self, llm, cfg: dict | None = None, prompt_path: str = "prompts/query_plan.yaml"):
        self.llm = llm
        self.cfg = cfg or {}
        self.prompt_path = prompt_path
        self.call_count = 0

    def plan(self, lv2: str, definition: str, strategy: dict,
             recent_seeds: list[dict], low_performers: list[str] | None = None,
             query_kind: str = "") -> list[QueryPlan]:
        if not self.llm or not self.cfg.get("enabled", True):
            return []
        rules = strategy.get("planner") or {}
        sources = _searchable_sources(strategy)
        if not sources:
            return []
        max_total = int(self.cfg.get("max_queries_per_lv2", 12))
        seeds = recent_seeds[: int(self.cfg.get("max_seed_items", 20))]
        spec = _plan_spec(self.prompt_path)
        system = spec.system_prompt.format(
            target_lv2=lv2, definition=definition,
            target_types=", ".join(strategy.get("target_types") or {}),
            sources=", ".join(f"{s['id']}({s.get('domain', 'web')})" for s in sources),
            query_kinds=", ".join([query_kind] if query_kind else rules.get("query_kinds") or ["event"]),
            required_context=", ".join(rules.get("required_context") or ["incident"]),
            forbidden_intents=", ".join(rules.get("forbidden_intents") or ["없음"]),
            recency_days=strategy.get("recency_days", 365), max_queries=max_total,
        )
        user = spec.user_prompt.format(
            seeds="\n".join(
                f"- {s.get('title', '')} ({s.get('published_at') or '날짜미상'}, {s.get('site_name', '')})"
                for s in seeds) or "- 없음",
            low_performers="\n".join(f"- {q}" for q in (low_performers or [])) or "- 없음",
        )
        self.call_count += 1
        data = self.llm._complete_json(system, user, _SCHEMA, spec.condition, max_tokens=2048)
        if not data:
            log.warning("query planner returned no JSON for %s: %s",
                        lv2, getattr(self.llm, "last_error", ""))
            return []
        plans = [
            QueryPlan(
                query=(item.get("query") or "").strip(), target_lv2=lv2,
                target_type=item.get("target_type", ""), query_kind=item.get("query_kind", ""),
                source_id=item.get("source_id", ""),
                expected_korea_evidence=list(item.get("expected_korea_evidence") or []),
                expected_lv2_evidence=list(item.get("expected_lv2_evidence") or []),
            )
            for item in data.get("queries", [])
        ]
        return apply_budgets([p for p in plans if validate_query_plan(p, strategy)],
                             strategy, max_total)
