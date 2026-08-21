"""2차 targeted 검색 계획 생성 — OpenAI를 쓰는 유일한 지점.

책임은 하나다: LV2·Type·최근 seed를 보고 '어디서 무엇을 검색할지'를 만든다.
본문 분류는 하지 않는다(그건 acceptance.evaluate의 몫).

LLM 출력은 그대로 실행하지 않는다.
  - validate_query_plan()이 미등록 type/source, 빈 evidence를 걸러낸다
  - forbidden_intents는 프롬프트에 더해 문자열 규칙으로 한 번 더 차단한다
    (안전 규칙 준수를 모델에 맡기지 않는다)
  - type별 query_budget / max_queries_per_lv2로 절단한다(저장량은 수집을 막지 않는다)

계획은 매 실행마다 새로 만들지 않는다. should_regenerate() 참고.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date
from functools import lru_cache

from src.common.prompt_loader import PromptSpec
from src.phase2.intent_builder import _HIGH_RISK_TERMS

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

# 지명은 검색어에 넣지 않는다. 특정 시·도를 박으면 그 지역 사건만 걸리는데,
# 어디서 사건이 났는지는 검색해 봐야 아는 것이라 범위를 미리 좁히면 손해다.
# 한국 한정은 "한국/국내" + 기관명·제도명으로 건다.
_SIDO = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원",
         "충북", "충남", "전북", "전남", "경북", "경남", "제주", "충청", "전라", "경상")
_FACILITY = ("월성", "고리", "한울", "한빛", "새울")     # 특정 원전도 범위를 좁히는 건 마찬가지
# '~시'가 시각·경우를 뜻하는 말은 지명이 아니다(사고시 대응, 고시 개정…)
_NOT_PLACE = {
    "사고시", "발생시", "조사시", "점검시", "운영시", "실시", "감시", "순시", "일시", "임시",
    "즉시", "동시", "당시", "평시", "전시", "응시", "제시", "표시", "명시", "무시", "경시",
    "중시", "주시", "묵시", "암시", "고시", "공시", "게시", "약시", "근시", "원시",
}
_CITY = re.compile(r"(?<![가-힣])([가-힣]{2})시(?![가-힣])")

# 법률 안내·집계 보도를 부르는 표현. 프롬프트로 금지해도 모델이 계속 쓴다
# (실측 2026-08-20: '대응 방법'을 금지했더니 '법적 대응 사례'로 바꿔 냈다).
# 지명과 같은 방식으로 코드가 막는다.
_ADVISORY_TERMS = (
    "대응 방법", "대응 사례", "대응 과정", "대응 조치", "법적 대응", "처벌 기준",
    "상담", "절차", "하는 법", "체크리스트", "해당할까", "가능할까", "알아보",
    "정리", "총정리", "안내",
)
_AGGREGATE_TERMS = ("실태조사", "설문", "비율", "종합대책", "통계", "현황", "백서")


# 한국 한정어. 검색어에 이게 없으면 provider가 어느 나라 기사든 가져온다.
# 실측(2026-08-20): 'SNS에서의 명예훼손 사건 보도'로 검색하니 후보 27건 중 18건이
# abcnews·CNN·reuters였다. 프롬프트 규칙 2)가 이미 요구하는데 모델이 지키지 않는다.
# 도메인 화이트리스트로 막으면 지역지·전문지가 통째로 빠지므로 검색어 쪽에서 건다.
_KOREA_MARKERS = (
    "한국", "국내", "대한민국", "우리나라",
    "경찰", "검찰", "법원", "대법원", "헌법재판소", "방통위", "방송통신심의위원회",
    "여성가족부", "교육청", "교육부", "국회", "정부", "지자체", "공정위", "개인정보보호위",
)


def korea_marker_in(query: str) -> str:
    """검색어에 든 한국 한정어를 돌려준다(없으면 빈 문자열)."""
    for term in _KOREA_MARKERS:
        if term in query:
            return term
    return ""


def pin_korea(plans: list[QueryPlan], strategy: dict) -> list[QueryPlan]:
    """한국 한정어가 없는 검색어 앞에 '국내'를 붙인다.

    버리지 않고 붙이는 이유: 손으로 쓴 폴백 검색어 216개 중 177개(82%)에 한정어가 없어
    버리면 폴백 경로가 통째로 죽는다. 게다가 그 검색어들도 같은 문제를 안고 있다 —
    한정어가 없으면 provider가 어느 나라 기사든 가져온다.
    """
    if not (strategy.get("planner") or {}).get("require_korea_marker", True):
        return plans
    return [p if korea_marker_in(p.query) else replace(p, query=f"국내 {p.query}")
            for p in plans]


def advisory_term_in(query: str) -> str:
    """검색어에 든 '법률 안내·집계 보도 유인' 표현을 돌려준다(없으면 빈 문자열)."""
    for term in _ADVISORY_TERMS + _AGGREGATE_TERMS:
        if term in query:
            return term
    return ""

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


def place_name_in(query: str) -> str:
    """검색어에 든 지명을 돌려준다(없으면 빈 문자열)."""
    for name in _SIDO + _FACILITY:
        if name in query:
            return name
    for match in _CITY.finditer(query):
        if match.group(0) not in _NOT_PLACE:
            return match.group(0)
    return ""


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
    if (strategy.get("planner") or {}).get("forbid_advisory_phrasing", True):
        advisory = advisory_term_in(plan.query)
        if advisory:
            log.info("query plan blocked by advisory phrasing '%s': %s", advisory, plan.query)
            return False
    if (strategy.get("planner") or {}).get("forbid_place_names", True):
        place = place_name_in(plan.query)
        if place:
            log.warning("query plan blocked by place name '%s': %s", place, plan.query)
            return False
    return bool(plan.expected_korea_evidence and plan.expected_lv2_evidence)


def apply_budgets(plans: list[QueryPlan], strategy: dict, max_total: int) -> list[QueryPlan]:
    """type별 query_budget = 검색 호출 예산. 수집 목표(부족분 랭킹용)와는 다른 축이다."""
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


def max_queries(cfg: dict) -> int:
    """이번 실행에서 살 검색어 상한. 검색 크레딧을 묶는 유일한 손잡이다.

    예전에는 '저장 목표 - 이미 모은 수'로 살 개수를 줄였는데, 목표를 채운 LV2는
    검색어를 0개 사서 아무 일도 안 하고 끝났다(화면에는 "완료"만 남아 오류로 읽혔다).
    목표는 부족분 랭킹에만 쓰고, 수집을 막지 않는다.
    """
    return int(cfg.get("max_queries_per_lv2", 12))


def _mix_sequence(mix: dict, valid: list[str], n: int) -> list[str]:
    """가중치대로 source id를 n개 배열한다. 몰아 넣지 않고 **번갈아** 낸다.

    뒤에서 apply_budgets가 앞에서부터 자르기 때문에, 한 source를 앞에 몰면 잘린 뒤
    비율이 무너진다(예전 primary_then_secondary가 정확히 그래서 Tavily만 나갔다).
    매 자리마다 '배정량/가중치'가 가장 뒤처진 source를 골라 비율을 유지한다.
    """
    weights = {sid: float(w) for sid, w in mix.items() if sid in valid and float(w) > 0}
    if not weights:
        return []
    assigned = dict.fromkeys(weights, 0.0)
    out = []
    for _ in range(n):
        sid = min(weights, key=lambda k: (assigned[k] / weights[k], list(weights).index(k)))
        assigned[sid] += 1.0
        out.append(sid)
    return out


def balance_sources(plans: list[QueryPlan], strategy: dict) -> list[QueryPlan]:
    """어느 채널로 검색할지를 코드가 정한다. 의미 판단이 아니라 채널 분배이기 때문이다.

    모델에 맡기면 프롬프트에 소스를 나열해도 1순위 하나로 다 몰아준다(실측 2026-08-19 1_A:
    40개 전부 web_news → 뉴스 SerpAPI 소스가 한 번도 호출되지 않았다).

    planner.source_mix가 있으면 그 가중치대로 나눈다(예: {web_news: 70, news_sites: 30}).
    없으면 같은 access끼리 고르게 나눈다. 어느 쪽이든 예산과 무관하게 모든 채널이 돈다.
    같은 access끼리만 섞는다 — 6_O처럼 라운드1(metadata_only 공식기관)과
    라운드2(direct 뉴스)가 나뉜 전략에서 두 라운드를 뒤섞으면 안 되기 때문이다.
    """
    if not plans:
        return plans
    mix = (strategy.get("planner") or {}).get("source_mix") or {}
    if mix:
        by_id = {s["id"]: s for s in _searchable_sources(strategy)}
        # 가중치에 적힌 source끼리 access가 갈리면 라운드를 뒤섞게 되므로 다수 access만 남긴다.
        accesses = Counter(by_id[sid].get("access", "direct") for sid in mix if sid in by_id)
        if accesses:
            keep = accesses.most_common(1)[0][0]
            valid = [sid for sid in mix if sid in by_id
                     and by_id[sid].get("access", "direct") == keep]
            sequence = _mix_sequence(mix, valid, len(plans))
            if sequence:
                return [replace(plan, source_id=sid) for plan, sid in zip(plans, sequence)]
    if len({p.source_id for p in plans}) > 1:
        return plans
    chosen = next((s for s in strategy.get("sources") or []
                   if s["id"] == plans[0].source_id), None)
    if chosen is None:
        return plans
    pool = [s for s in _searchable_sources(strategy)
            if s.get("access", "direct") == chosen.get("access", "direct")]
    if len(pool) < 2:
        return plans
    return [
        replace(plan, source_id=pool[i % len(pool)]["id"])
        for i, plan in enumerate(plans)
    ]


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


def _source_for_type(sources: list[dict], domains: list[str]) -> str:
    """type 담당 도메인과 일치하는 source를 고른다. 없으면 우선순위 1순위."""
    for domain in domains:
        for source in sources:
            if source.get("domain") and source["domain"] in domain:
                return source["id"]
    return sources[0]["id"]


def fallback_plans(lv2: str, strategy: dict, queries_by_type: dict,
                   include_by_type: dict | None = None,
                   domains_by_type: dict | None = None) -> list[QueryPlan]:
    """LLM 실패 시 기존 collection_intents_by_lv2 검색어를 계획으로 승격한다.

    domains_by_type(= serpapi 규칙)이 있으면 type별 담당 기관으로 배분한다.
    이걸 안 쓰면 생물·핵·폭발물이 전부 1순위 소스 하나로 몰린다.
    """
    sources = _searchable_sources(strategy)
    if not sources:
        return []
    include_by_type = include_by_type or {}
    domains_by_type = domains_by_type or {}
    plans = [
        QueryPlan(
            query=query, target_lv2=lv2, target_type=target_type, query_kind="event",
            source_id=_source_for_type(sources, domains_by_type.get(target_type) or []),
            expected_korea_evidence=["한국", "국내"],
            expected_lv2_evidence=include_by_type.get(target_type) or [target_type],
            generation_source="config_fallback",
        )
        for target_type in (strategy.get("target_types") or {})
        for query in queries_by_type.get(target_type, [])
    ]
    return pin_korea([p for p in plans if validate_query_plan(p, strategy)], strategy)


class QueryPlanner:
    def __init__(self, llm, cfg: dict | None = None, prompt_path: str = "prompts/query_plan.yaml"):
        self.llm = llm
        self.cfg = cfg or {}
        self.prompt_path = prompt_path
        self.call_count = 0

    def plan(self, lv2: str, definition: str, strategy: dict,
             recent_seeds: list[dict], low_performers: list[str] | None = None,
             query_kind: str = "", max_queries: int | None = None) -> list[QueryPlan]:
        if not self.llm or not self.cfg.get("enabled", True):
            return []
        rules = strategy.get("planner") or {}
        sources = _searchable_sources(strategy)
        if not sources:
            return []
        max_total = int(max_queries or self.cfg.get("max_queries_per_lv2", 12))
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
        # 검색어 1건이 대략 130토큰(query+type+kind+source+evidence 2줄)이다. 고정 2048로 두면
        # 검색어 16개쯤에서 JSON이 잘려 파싱에 실패하고 조용히 config_fallback으로 떨어진다
        # (실측 2026-08-19: 68개 요청 → char 6523에서 절단 → 고정 검색어 15개만 실행).
        data = self.llm._complete_json(system, user, _SCHEMA, spec.condition,
                                       max_tokens=min(16000, 1024 + 130 * max_total))
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
        return balance_sources(
            pin_korea(apply_budgets([p for p in plans if validate_query_plan(p, strategy)],
                                    strategy, max_total), strategy), strategy)
