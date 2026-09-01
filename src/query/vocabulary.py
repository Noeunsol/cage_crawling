"""검색어 생성에 쓸 search_vocabulary를 정적/최신/폴백 순서로 조립하고, 수집 단계에서 쓸
exclude_criteria를 고른다 — 둘 다 taxonomy.yaml의 새 필드(search_vocabulary, collection_exclude_criteria,
lv2_fallback_vocabulary)가 없는 type에서도 기존 exclude_criteria만으로 동작해야 한다(하위 호환).
"""

from __future__ import annotations

import re

VOCAB_STATES = ("static_and_fresh", "static_only", "fresh_only", "definition_fallback", "lv2_fallback")

# definition/include_criteria에서 뽑아낸 조각 중 이 길이를 넘으면 "표현"이 아니라 원문 문장이 그대로
# 남은 것으로 보고 버린다 — 방법·절차를 서술하는 문장이 검색어 후보로 새지 않게 하는 안전장치.
_MAX_TERM_CHARS = 20
_SPLIT_PATTERN = re.compile(r"[·,、/]|\s+등\s+|\s+및\s+")


def find_lv2(configs: dict, lv2_id: str) -> dict | None:
    for lv2 in configs["taxonomy"]["taxonomy"]:
        if lv2["lv2_id"] == lv2_id:
            return lv2
    return None


def find_type(configs: dict, lv2_id: str, type_name: str) -> dict | None:
    """ui.common.find_type과 동일한 조회지만, src/ 쪽 코드(테스트 포함)가 ui를 import하지 않고도
    쓸 수 있도록 여기 둔다."""
    lv2 = find_lv2(configs, lv2_id)
    if lv2 is None:
        return None
    for t in lv2["types"]:
        if t["name"] == type_name:
            return t
    return None


def effective_exclude_criteria(type_cfg: dict) -> list[str]:
    """collection_exclude_criteria가 있으면 그걸, 없으면 기존 exclude_criteria를 쓴다."""
    return type_cfg.get("collection_exclude_criteria") or type_cfg["exclude_criteria"]


def derive_safe_terms_from_definition(*, definition: str, include_criteria: list[str]) -> list[str]:
    """LLM 호출 없이 definition/include_criteria(이미 검수된 taxonomy 문구)를 구분자로 쪼개서
    짧은 표현만 추린다. 새로운 단어를 만들어내지 않으므로 위험한 방법·도구가 생성될 여지가 없다.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for text in [definition, *include_criteria]:
        for piece in _SPLIT_PATTERN.split(text):
            piece = piece.strip(" .")
            if not piece or len(piece) > _MAX_TERM_CHARS or piece in seen:
                continue
            seen.add(piece)
            terms.append(piece)
    return terms


def get_lv2_fallback_vocabulary(configs: dict, lv2_id: str) -> list[str]:
    lv2 = find_lv2(configs, lv2_id)
    return lv2.get("lv2_fallback_vocabulary", []) if lv2 else []


def build_vocabulary(*, configs: dict, lv2_id: str, type_cfg: dict, fresh_terms: list[str]) -> tuple[list[str], str]:
    """(merged_vocabulary, state)를 돌려준다. state는 VOCAB_STATES 중 하나 — 호출 쪽에서 로그로 남긴다."""
    static_terms = type_cfg.get("search_vocabulary") or []
    fresh_terms = fresh_terms or []
    merged = list(dict.fromkeys([*static_terms, *fresh_terms]))
    if merged:
        state = "static_and_fresh" if static_terms and fresh_terms else ("static_only" if static_terms else "fresh_only")
    else:
        derived = derive_safe_terms_from_definition(
            definition=type_cfg["definition"], include_criteria=type_cfg.get("include_criteria", []),
        )
        merged, state = (derived, "definition_fallback") if derived else (
            get_lv2_fallback_vocabulary(configs, lv2_id), "lv2_fallback",
        )
    assert state in VOCAB_STATES
    return merged, state


def resolve_vocabulary(
    *, client, fresh_prompt_cfg: dict, fresh_vocab_repo, freshness_module, conn, configs: dict,
    lv2_id: str, type_name: str, type_cfg: dict, on_fresh_usage=None,
) -> tuple[list[str], str, Exception | None]:
    """type 하나의 최종 search_vocabulary를 만든다: 캐시 조회 → 미스면 웹서치 1회 → 정적/최신/폴백 병합.

    UI(query_review.py)가 이 오케스트레이션을 직접 들고 있지 않도록 여기로 모았다 — 웹서치 실패는
    예외로 던지지 않고 세 번째 반환값으로 돌려주니, 호출 쪽은 그걸로 경고만 띄우면 된다.
    """
    fresh_terms = fresh_vocab_repo.get_cached(conn, lv2_id, type_name)
    error: Exception | None = None
    if fresh_terms is None:
        try:
            fresh_result = freshness_module.fetch_fresh_vocabulary(
                client, fresh_prompt_cfg, type_name=type_name, definition=type_cfg["definition"],
            )
            fresh_terms = getattr(fresh_result, "terms", fresh_result)
            if on_fresh_usage is not None and hasattr(fresh_result, "prompt_tokens"):
                on_fresh_usage(fresh_result)
            fresh_vocab_repo.save(conn, lv2_id, type_name, fresh_terms)
        except Exception as e:  # 웹서치 실패는 치명적이지 않다 — 정적 vocabulary만으로 계속 진행
            error = e
            fresh_terms = []

    merged, state = build_vocabulary(configs=configs, lv2_id=lv2_id, type_cfg=type_cfg, fresh_terms=fresh_terms)
    return merged, state, error
