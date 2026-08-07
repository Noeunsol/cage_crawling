"""taxonomy.yaml 기준 → 자연어 collection intent 변환 (2차 semantic discovery의 핵심).

taxonomy label을 검색어로 쓰지 않는다. definition + include/exclude signals + source preference를
자연어 collection intent로 조립해 semantic provider(Tavily)에 넣는다.

우선순위: phase2 config의 `collection_intents_by_lv2[lv2]`(수동 intent)가 있으면 사용,
없으면 taxonomy.yaml 필드에서 파생. `sensitive_overlay[lv2]`가 있으면 맥락을 좁히고 제외를 추가한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CollectionIntent:
    target_taxonomy_lv2: str
    natural_language_query: str          # provider에 넣는 자연어 intent
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    korea_relevance_requirement: str = ""
    preferred_source_types: list[str] = field(default_factory=list)
    preferred_domains: list[str] = field(default_factory=list)   # (선택) fallback 힌트, 중심 아님
    max_results: int = 10
    freshness_days: int | None = None
    force_review: bool = False           # sensitive_overlay: 최종 accepted를 review로 강등
    is_manual: bool = False              # config에서 사람이 검색 목적을 직접 설계했는지


_KOREA_REQUIREMENT = "한국어 본문, 한국 플랫폼, 한국 사건/제도/피해 맥락 중 하나 이상이 있어야 함"


def _dedup(items) -> list[str]:
    return list(dict.fromkeys(x for x in items if x))


def build_collection_intent(policy, config: dict) -> CollectionIntent:
    lv2 = policy.taxonomy_lv2
    manual = (config.get("collection_intents_by_lv2") or {}).get(lv2)
    overlay = (config.get("sensitive_overlay") or {}).get(lv2)
    source_pref = (config.get("source_preference") or {}).get(lv2, {})
    tavily = (config.get("providers") or {}).get("tavily", {})
    max_results = int(tavily.get("max_results_per_query", 10))

    if manual:
        goal = " ".join((manual.get("goal") or "").split())
        include = _dedup(manual.get("include", []))
        exclude = _dedup(manual.get("exclude", []))
        korea_required = bool(manual.get("korea_required", True))
        domains = manual.get("preferred_domains", []) or source_pref.get("domains", [])
    else:
        # taxonomy.yaml 파생: 전 subtype의 keywords/positive → include, negative → exclude
        include = _dedup(
            k for st in policy.subtypes for k in (list(st.keywords) + list(st.positive_patterns))
        )
        exclude = _dedup(n for st in policy.subtypes for n in st.negative_patterns)
        exclude = _dedup(exclude + ["단순 정의 설명", "광고", "홍보", "위키성 문서", "처리방침"])
        definition = " ".join((policy.definition or policy.description or "").split())
        goal = f"{definition} 이런 행위/피해가 실제로 드러나는 한국어 콘텐츠를 찾는다."
        korea_required = True
        domains = source_pref.get("domains", [])

    # sensitive_overlay: 수집 금지가 아니라 '맥락 제한' + 강제 review
    force_review = False
    if overlay:
        force_review = bool(overlay.get("force_review", True))
        allowed = overlay.get("allowed_content_types", [])
        disallowed = overlay.get("disallowed_intents", [])
        if allowed:
            goal += f" 단, {', '.join(allowed)} 맥락(뉴스·기관·정책·사고 보도 등)만 수집한다."
        if disallowed:
            exclude = _dedup(exclude + [f"{d}(제조/합성/조달/실행 방법)" for d in disallowed])

    korea_req = _KOREA_REQUIREMENT if korea_required else ""
    nlq = goal
    if include:
        nlq += " 포함 맥락: " + ", ".join(include[:12]) + "."
    if exclude:
        nlq += " 제외: " + ", ".join(exclude[:12]) + "."
    if korea_req:
        nlq += f" 조건: {korea_req}."

    return CollectionIntent(
        target_taxonomy_lv2=lv2,
        natural_language_query=" ".join(nlq.split()),
        include=include,
        exclude=exclude,
        korea_relevance_requirement=korea_req,
        preferred_source_types=source_pref.get("source_types", []),
        preferred_domains=domains,
        max_results=max_results,
        freshness_days=source_pref.get("freshness_days"),
        force_review=force_review,
        is_manual=bool(manual),
    )


def validate_collection_intent(intent: CollectionIntent) -> dict:
    """API 호출 전 intent lint. 검색 결과 품질은 Preview/본문 target-match로 최종 확인한다."""
    warnings = []
    score = 100
    if not intent.is_manual:
        warnings.append("taxonomy 키워드 자동 조합: 실제 검색 문장 수동 검토 필요")
        score -= 15
    if len(intent.include) > 12:
        warnings.append(f"include {len(intent.include)}개 중 앞 12개만 쿼리에 반영됨")
        score -= 10
    if len(intent.natural_language_query) > 350:
        warnings.append(f"쿼리가 김({len(intent.natural_language_query)}자): 핵심 사례 중심으로 축약 권장")
        score -= 10
    if len(intent.exclude) < 2:
        warnings.append("제외 맥락이 부족함")
        score -= 15
    if not intent.korea_relevance_requirement:
        warnings.append("한국 관련성 조건 없음")
        score -= 20
    concrete_terms = ("피해", "사례", "사건", "호소", "질문", "게시글", "콘텐츠", "보도", "상담")
    if not any(term in intent.natural_language_query for term in concrete_terms):
        warnings.append("실제 사건·피해·상담 콘텐츠를 요구하는 표현이 약함")
        score -= 15
    score = max(0, score)
    return {"score": score, "status": "good" if score >= 80 else "review" if score >= 60 else "weak",
            "warnings": warnings}


if __name__ == "__main__":
    from ..policy import load_policies

    policies = {p.taxonomy_lv2: p for p in load_policies("configs/taxonomy.yaml")}
    cfg = {
        "collection_intents_by_lv2": {
            "4_I_Privacy_Infringement": {
                "goal": "한국어 커뮤니티·Q&A에서 특정인의 개인정보가 동의 없이 공개되어 피해를 호소하는 콘텐츠를 찾는다.",
                "include": ["피해 호소", "고소 가능성 질문"],
                "exclude": ["개인정보보호법 단순 설명", "광고"],
                "korea_required": True,
                "preferred_domains": ["kin.naver.com"],
            }
        },
        "sensitive_overlay": {
            "6_O_CBRNE": {"allowed_content_types": ["news_case"], "disallowed_intents": ["manufacturing"], "force_review": True}
        },
        "providers": {"tavily": {"max_results_per_query": 8}},
    }
    # 수동 intent
    m = build_collection_intent(policies["4_I_Privacy_Infringement"], cfg)
    assert "개인정보" in m.natural_language_query and "고소 가능성 질문" in m.natural_language_query
    assert "4_I_Privacy_Infringement" not in m.natural_language_query, "label이 쿼리에 그대로 들어가면 안 됨"
    assert m.max_results == 8 and not m.force_review

    # 파생 intent (config에 없는 LV2)
    d = build_collection_intent(policies["2_F_Bias_and_Hate"], cfg)
    assert d.include and "제외" in d.natural_language_query

    # sensitive_overlay: force_review + 맥락 제한
    s = build_collection_intent(policies["6_O_CBRNE"], cfg)
    assert s.force_review and "news_case" in s.natural_language_query
    assert any("manufacturing" in e for e in s.exclude)
    assert validate_collection_intent(m)["status"] == "good"
    assert validate_collection_intent(d)["warnings"], "자동 파생 intent는 수동 검토 경고가 필요"
    print("intent_builder self-check OK")
