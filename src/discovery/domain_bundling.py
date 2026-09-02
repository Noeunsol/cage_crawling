"""type당 SerpAPI 도메인을 최대 3개씩 균등하게 교차 조합하는 순수 로직.

SerpAPI 한 번의 검색에는 도메인을 몇 개까지만 site: 로 묶어서 보낸다. 여기서는
"주어진 도메인 목록을 번들로 어떻게 나눌지"만 계산한다. 어떤 번들을 언제 쓸지(LRU, DB 기록)는
storage/repositories/domain_bundles.py가 담당한다.
"""

from __future__ import annotations

MAX_DOMAINS_PER_BUNDLE = 3


def build_bundles(domains: list[str], alias_groups: dict[str, list[str]]) -> list[list[str]]:
    """원래 순서를 한 칸씩 이동하며 모든 도메인이 비슷한 횟수로 포함되게 묶는다.

    예: A,B,C,D -> ABC, BCD, CDA, DAB. alias 관계인 도메인은 항상 함께 이동한다.
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

    if not units:
        return []
    if sum(map(len, units)) <= MAX_DOMAINS_PER_BUNDLE:
        return [[domain for unit in units for domain in unit]]

    bundles: list[list[str]] = []
    for start in range(len(units)):
        bundle: list[str] = []
        for offset in range(len(units)):
            unit = units[(start + offset) % len(units)]
            # bundle이 비어있을 때(= 이번 번들의 첫 unit)는 크기 체크를 건너뛴다 — alias 그룹은
            # 항상 함께 묶여야 하므로(위 docstring), 그 그룹 하나가 MAX_DOMAINS_PER_BUNDLE보다
            # 커도 쪼갤 수 없다. 이 경우 번들이 "최대 3개"를 넘는 게 그룹을 깨는 것보다 낫다.
            # domain_aliases.yaml이 비어 있는 한 지금은 발생하지 않는다.
            if bundle and len(bundle) + len(unit) > MAX_DOMAINS_PER_BUNDLE:
                break
            bundle.extend(unit)
        if bundle not in bundles:
            bundles.append(bundle)
    return bundles
