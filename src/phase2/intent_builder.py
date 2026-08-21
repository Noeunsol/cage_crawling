"""수집 전략 config → Tavily collection intent (2차 semantic discovery의 핵심).

taxonomy label을 검색어로 쓰지 않는다. targeted_collection.yaml의 `queries`(사람이 설계한
짧은 검색 구문)를 그대로 provider에 넣고, include/exclude는 쿼리가 아니라 rerank 신호로만 쓴다.
Tavily에 부정 연산자가 없어 제외어를 쿼리에 넣으면 오히려 그 문서를 불러오기 때문이다.

원칙: 모든 LV2는 config에 수동 intent를 갖는다. 없으면 taxonomy.yaml에서 파생하되(파이프라인이
깨지지 않도록) 경고한다. 강제는 런타임이 아니라 test_targeted.py가 한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from itertools import zip_longest

log = logging.getLogger(__name__)


@dataclass
class CollectionIntent:
    target_taxonomy_lv2: str
    queries: list[str] = field(default_factory=list)   # 실제 Tavily 검색어. 각각 1회 호출.
    query_types: dict[str, str] = field(default_factory=dict)
    include_by_type: dict[str, list[str]] = field(default_factory=dict)
    missing_types: list[str] = field(default_factory=list)
    dropped_queries: list[str] = field(default_factory=list)   # max_searches에 잘려나간 쿼리
    include: list[str] = field(default_factory=list)   # rerank 가점 전용
    exclude: list[str] = field(default_factory=list)   # rerank 감점 전용
    event_terms: list[str] = field(default_factory=list)  # 대상 신호와 함께 있어야 하는 사건·피해 신호
    korea_relevance_requirement: str = ""
    excluded_domains: list[str] = field(default_factory=list)
    max_results: int = 10
    max_searches: int = 4                # 이 LV2의 Tavily 호출 상한(넓은 LV2는 config에서 올린다)
    force_review: bool = False           # sensitive_overlay: 최종 accepted를 review로 강등
    is_sensitive: bool = False           # sensitive_overlay 대상 → high-risk 쿼리 검사
    is_manual: bool = False              # config에서 사람이 검색 목적을 직접 설계했는지


_KOREA_REQUIREMENT = "한국어 본문, 한국 플랫폼, 한국 사건/제도/피해 맥락 중 하나 이상이 있어야 함"

# 민감 LV2 쿼리에 있으면 안 되는 어휘. 방법론·조달·유통 검색으로 흐르는 순간 수집 자체가 사고다.
_HIGH_RISK_TERMS = (
    "제조", "합성", "만드는 법", "만드는법", "제작법", "제조법", "레시피",
    "조달", "구매", "구입", "판매", "다운로드", "공유", "링크", "텔레그램", "토렌트",
)


def _dedup(items) -> list[str]:
    return list(dict.fromkeys(x for x in items if x))


def _warn_unknown_types(lv2: str, policy, by_type: dict) -> list[str]:
    """queries_by_type의 키가 taxonomy.yaml의 실제 type 이름인지. 오타는 조용히 넘어가면 안 된다."""
    known = {st.name for st in policy.subtypes}
    unknown = [t for t in by_type if t not in known]
    if unknown:
        log.warning("%s: queries_by_type에 없는 type %s (taxonomy type: %s)",
                    lv2, unknown, sorted(known))
    missing = sorted(known - set(by_type))
    if missing:
        log.warning("%s: queries_by_type에 없는 taxonomy type %s", lv2, missing)
    return missing


def build_collection_intent(policy, config: dict) -> CollectionIntent:
    lv2 = policy.taxonomy_lv2
    manual = (config.get("collection_intents_by_lv2") or {}).get(lv2)
    overlay = (config.get("sensitive_overlay") or {}).get(lv2)
    tavily = (config.get("providers") or {}).get("tavily", {})
    max_results = int(tavily.get("max_results_per_query", 10))
    max_searches = int(tavily.get("max_searches_per_lv2", 4))
    global_excluded_domains = _dedup(tavily.get("excluded_domains", []))

    if manual:
        # queries(LV2 공통) + queries_by_type(넓은 LV2의 type별 분기). 한 쿼리로 뭉치면 획일화된다.
        by_type = manual.get("queries_by_type") or {}
        missing_types = _warn_unknown_types(lv2, policy, by_type) if by_type else []
        # type별 쿼리는 라운드로빈으로 섞는다. 그냥 이어붙이면 max_searches에 잘릴 때
        # 뒤쪽 type이 통째로 0건이 된다 — 상한은 type을 버리지 말고 깊이만 줄여야 한다.
        typed_rounds = [item for row in zip_longest(
            *[[(q, subtype) for q in qs] for subtype, qs in by_type.items()]
        ) for item in row if item]
        query_types = {}
        for query, subtype in [*((q, "") for q in manual.get("queries") or []), *typed_rounds]:
            query = " ".join(str(query).split())
            if query:
                query_types.setdefault(query, subtype)
        queries = list(query_types)
        max_searches = int(manual.get("max_searches", max_searches))
        include = _dedup(manual.get("include", []))
        include_by_type = {subtype: _dedup(terms) for subtype, terms
                           in (manual.get("include_by_type") or {}).items()}
        exclude = _dedup(manual.get("exclude", []))
        event_terms = _dedup(manual.get("event_terms", []))
        korea_required = bool(manual.get("korea_required", True))
        excluded_domains = _dedup(global_excluded_domains + manual.get("excluded_domains", []))
    else:
        # taxonomy.yaml 파생(fallback): definition 한 문장만. 키워드 나열은 검색 품질을 망친다.
        definition = " ".join((policy.definition or policy.description or "").split())
        queries = [f"{definition[:120]} 한국 사례"] if definition else [policy.taxonomy_lv2_name or lv2]
        include = _dedup(k for st in policy.subtypes for k in st.keywords)
        include_by_type = {}
        exclude = _dedup(
            [n for st in policy.subtypes for n in st.negative_patterns]
            + ["단순 정의 설명", "광고", "홍보", "위키성 문서", "처리방침"]
        )
        event_terms = []
        korea_required = True
        excluded_domains = global_excluded_domains
        missing_types = []
        query_types = {queries[0]: ""}

    # sensitive_overlay: 수집 금지가 아니라 '맥락 제한'(queries 문안이 담당) + 강제 review
    force_review = False
    if overlay:
        force_review = bool(overlay.get("force_review", True))
        disallowed = overlay.get("disallowed_intents", [])
        if disallowed:
            exclude = _dedup(exclude + [f"{d}(제조/합성/조달/실행 방법)" for d in disallowed])

    # 비용 상한. 자를 때 조용히 넘어가지 않는다 — 로그 + intent에 기록해 UI/dry-run에서도 보이게 한다.
    dropped_queries = queries[max_searches:]
    if dropped_queries:
        # 전략(검색 계획) LV2는 이 고정 검색문을 아예 실행하지 않는다. LLM이 실패했을 때의
        # 폴백일 뿐이라 "몇 개를 버렸다"고 경고하면 안 쓰는 검색어를 걱정하게 만든다.
        # preview_intents가 선택과 무관하게 19개를 다 만들기 때문에 화면이 이 경고로 덮인다.
        strategy_lv2 = lv2 in (config.get("source_strategies_by_lv2") or {})
        log.log(logging.DEBUG if strategy_lv2 else logging.WARNING,
                "%s: 손으로 쓴 쿼리 %d개 중 %d개만 사용(max_searches=%d)%s, 버림: %s",
                lv2, len(queries), max_searches, max_searches,
                " — 폴백 경로라 이번 실행에서는 쓰이지 않는다" if strategy_lv2 else "",
                dropped_queries)
        queries = queries[:max_searches]
    query_types = {query: query_types.get(query, "") for query in queries}

    return CollectionIntent(
        target_taxonomy_lv2=lv2,
        queries=queries,
        query_types=query_types,
        include_by_type=include_by_type,
        missing_types=missing_types,
        dropped_queries=dropped_queries,
        include=include,
        exclude=exclude,
        event_terms=event_terms,
        korea_relevance_requirement=_KOREA_REQUIREMENT if korea_required else "",
        excluded_domains=excluded_domains,
        max_results=max_results,
        max_searches=max_searches,
        force_review=force_review,
        is_sensitive=bool(overlay),
        is_manual=bool(manual),
    )


def missing_manual_intents(policies, config: dict) -> list[str]:
    """config에 수동 intent가 없는 LV2 코드. 런타임은 경고만 하고 파생 intent로 진행한다.

    강제는 tests/test_targeted.py가 한다 — 빌드 3분 뒤 유료 호출 중에 죽는 것보다 PR에서 죽는 게 낫다.
    그래서 `strict_intents` 같은 설정 knob은 두지 않는다.
    ranked(부족분 상위)가 아니라 policies 전체를 도는 것이 중요하다. 목표를 이미 채운 LV2는
    rank_deficits에서 빠져 preview에 안 나타나므로 누락을 영영 못 본다.
    """
    manual = config.get("collection_intents_by_lv2") or {}
    return [p.taxonomy_lv2 for p in policies if p.taxonomy_lv2 not in manual]


def validate_collection_intent(intent: CollectionIntent) -> dict:
    """API 호출 전 intent lint. 검색 결과 품질은 Preview/본문 target-match로 최종 확인한다.

    status=blocked는 민감 LV2 전용 하드 게이트다. 호출자는 blocked intent를 실행하면 안 된다.
    """
    warnings = []
    score = 100

    # 민감 LV2: 방법론·조달 어휘가 쿼리에 있으면 실행 금지
    if intent.is_sensitive:
        hits = _dedup(t for q in intent.queries for t in _HIGH_RISK_TERMS if t in q)
        if hits:
            return {"score": 0, "status": "blocked",
                    "warnings": [f"민감 LV2 쿼리에 금지 어휘 {hits}: 사건 보도·수사·정책 맥락으로 다시 쓸 것"]}

    if not intent.is_manual:
        warnings.append("taxonomy 자동 파생 intent: config에 수동 queries를 작성할 것")
        score -= 25
    if intent.missing_types:
        warnings.append(f"type별 쿼리 누락: {', '.join(intent.missing_types)}")
        score -= 20
    if not intent.queries:
        warnings.append("쿼리가 없음")
        score -= 40
    if intent.dropped_queries:   # 잘라낸 뒤라 개수 검사로는 안 잡힌다. config가 어긋난 상태.
        warnings.append(f"max_searches={intent.max_searches}에 걸려 쿼리 {len(intent.dropped_queries)}개 미사용")
        score -= 20
    long_q = [q for q in intent.queries if len(q) > 100]
    if long_q:
        warnings.append(f"쿼리가 김(>100자) {len(long_q)}개: 사건 중심으로 축약 권장")
        score -= 10
    if len(intent.exclude) < 2:
        warnings.append("제외 맥락이 부족함(rerank 감점 신호)")
        score -= 15
    if not intent.korea_relevance_requirement:
        warnings.append("한국 관련성 조건 없음")
        score -= 20
    concrete_terms = ("피해", "사례", "사건", "호소", "질문", "게시글", "보도", "상담", "논란", "신고")
    if not any(term in q for q in intent.queries for term in concrete_terms):
        warnings.append("실제 사건·피해·상담 콘텐츠를 요구하는 표현이 약함")
        score -= 15

    score = max(0, score)
    return {"score": score, "status": "good" if score >= 80 else "review" if score >= 60 else "weak",
            "warnings": warnings}


if __name__ == "__main__":
    from src.common.policy import load_policies

    all_policies = load_policies("configs/taxonomy.yaml")
    policies = {p.taxonomy_lv2: p for p in all_policies}
    cfg = {
        "collection_intents_by_lv2": {
            "4_I_Privacy_Infringement": {
                "queries": ["신상털이 피해 고소 가능성 질문", "개인정보 유출 커뮤니티 박제 피해 호소"],
                "include": ["피해 호소", "고소 가능성"],
                "exclude": ["개인정보보호법 단순 설명", "광고"],
                "korea_required": True,
            },
            "6_O_CBRNE": {
                "queries": ["폭발물 사고 수사 보도", "화학물질 유출 사고 규제 발표"],
                "include": ["사고", "수사"], "exclude": ["제품 홍보", "위키성 문서"],
                "korea_required": True,
            },
        },
        "sensitive_overlay": {
            "6_O_CBRNE": {"disallowed_intents": ["manufacturing"], "force_review": True}
        },
        "providers": {"tavily": {"max_results_per_query": 8, "max_searches_per_lv2": 2}},
    }

    # 수동 intent: queries가 그대로 나가고 exclude는 쿼리에 섞이지 않는다
    m = build_collection_intent(policies["4_I_Privacy_Infringement"], cfg)
    assert m.queries == ["신상털이 피해 고소 가능성 질문", "개인정보 유출 커뮤니티 박제 피해 호소"]
    assert not any("광고" in q for q in m.queries), "exclude는 쿼리에 들어가면 안 됨"
    assert not any("4_I_Privacy" in q for q in m.queries), "label이 쿼리에 그대로 들어가면 안 됨"
    assert m.max_results == 8 and not m.force_review and m.is_manual
    assert validate_collection_intent(m)["status"] == "good"

    # queries_by_type: 넓은 LV2를 type별로 쪼개고 LV2별 max_searches로 상한을 올린다
    wide = {**cfg, "collection_intents_by_lv2": {**cfg["collection_intents_by_lv2"],
            "2_F_Bias_and_Hate": {"queries": ["지역 비하 논란"], "max_searches": 3,
                                  "queries_by_type": {"gender": ["여성 혐오 게시물 논란"],
                                                      "disability": ["장애인 비하 논란"]},
                                  "include": ["혐오"], "exclude": ["캠페인", "논문"]}}}
    w = build_collection_intent(policies["2_F_Bias_and_Hate"], wide)
    assert w.queries == ["지역 비하 논란", "여성 혐오 게시물 논란", "장애인 비하 논란"]
    assert w.max_searches == 3 and validate_collection_intent(w)["status"] == "good"

    # 파생 intent(config에 없는 LV2): 쿼리 1개 + 수동 작성 경고
    d = build_collection_intent(policies["2_F_Bias_and_Hate"], cfg)
    assert len(d.queries) == 1 and d.include and not d.is_manual
    assert validate_collection_intent(d)["warnings"], "자동 파생 intent는 경고가 필요"

    # sensitive_overlay: force_review + disallowed → exclude
    s = build_collection_intent(policies["6_O_CBRNE"], cfg)
    assert s.force_review and s.is_sensitive and any("manufacturing" in e for e in s.exclude)
    assert validate_collection_intent(s)["status"] != "blocked"

    # 민감 LV2 high-risk 쿼리는 blocked
    bad_cfg = {**cfg, "collection_intents_by_lv2": {
        **cfg["collection_intents_by_lv2"],
        "6_O_CBRNE": {**cfg["collection_intents_by_lv2"]["6_O_CBRNE"],
                      "queries": ["폭발물 제조 방법"]}}}
    bad = build_collection_intent(policies["6_O_CBRNE"], bad_cfg)
    assert validate_collection_intent(bad)["status"] == "blocked"

    # max_searches_per_lv2 초과분은 잘린다
    many = {**cfg, "collection_intents_by_lv2": {
        **cfg["collection_intents_by_lv2"],
        "4_I_Privacy_Infringement": {**cfg["collection_intents_by_lv2"]["4_I_Privacy_Infringement"],
                                     "queries": ["a 피해", "b 피해", "c 피해"]}}}
    assert len(build_collection_intent(policies["4_I_Privacy_Infringement"], many).queries) == 2

    missing = missing_manual_intents(all_policies, cfg)
    assert "2_F_Bias_and_Hate" in missing and "4_I_Privacy_Infringement" not in missing
    print("intent_builder self-check OK")
