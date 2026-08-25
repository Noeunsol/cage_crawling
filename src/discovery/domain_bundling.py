"""type당 SerpAPI 도메인을 최대 3개씩 묶는 순수 로직.

SerpAPI 한 번의 검색에는 도메인을 몇 개까지만 site: 로 묶어서 보낸다. 여기서는
"주어진 도메인 목록을 번들로 어떻게 나눌지"만 계산한다. 어떤 번들을 언제 쓸지(LRU, DB 기록)는
storage/repositories/domain_bundles.py가 담당한다.
"""

from __future__ import annotations

MAX_DOMAINS_PER_BUNDLE = 3


def build_bundles(domains: list[str], alias_groups: dict[str, list[str]]) -> list[list[str]]:
    """domains를 원래 순서를 유지하면서 최대 MAX_DOMAINS_PER_BUNDLE개씩 묶는다.

    alias_groups[canonical] = [alias, ...] 관계에 있는 도메인들은 한 묶음의 크기를 넘지 않는 한
    항상 같은 번들에 들어간다 — 절대 다른 번들로 쪼개지 않는다.
    """
    canonical_of: dict[str, str] = {}
    for canonical, aliases in alias_groups.items():
        for d in [canonical, *aliases]:
            canonical_of[d] = canonical

    units: list[list[str]] = []
    seen: set[str] = set()
    for d in domains:
        if d in seen:
            continue
        group_key = canonical_of.get(d, d)
        unit = [x for x in domains if canonical_of.get(x, x) == group_key and x not in seen]
        units.append(unit)
        seen.update(unit)

    bundles: list[list[str]] = []
    for unit in units:
        if bundles and len(bundles[-1]) + len(unit) <= MAX_DOMAINS_PER_BUNDLE:
            bundles[-1].extend(unit)
        else:
            bundles.append(list(unit))
    return bundles
