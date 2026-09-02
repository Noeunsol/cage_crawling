"""도메인 교차 조합 검증: 모든 사이트를 균등 순환하고 alias는 쪼개지 않는다."""

from src.discovery.domain_bundling import build_bundles


def test_builds_deterministic_balanced_rolling_bundles():
    bundles = build_bundles(["d1", "d2", "d3", "d4"], alias_groups={})
    assert bundles == [
        ["d1", "d2", "d3"], ["d2", "d3", "d4"],
        ["d3", "d4", "d1"], ["d4", "d1", "d2"],
    ]
    assert {domain: sum(domain in b for b in bundles) for domain in ["d1", "d2", "d3", "d4"]} == {
        "d1": 3, "d2": 3, "d3": 3, "d4": 3,
    }


def test_no_domains_produces_no_bundles():
    assert build_bundles([], alias_groups={}) == []


def test_fewer_than_three_domains_is_one_bundle():
    assert build_bundles(["d1", "d2"], alias_groups={}) == [["d1", "d2"]]


def test_alias_group_never_split_across_bundles():
    domains = ["d1", "krcert.or.kr", "boho.or.kr", "d2"]
    alias_groups = {"krcert.or.kr": ["boho.or.kr"]}

    bundles = build_bundles(domains, alias_groups)

    # krcert.or.kr과 boho.or.kr은 항상 같은 번들에 있어야 한다.
    for bundle in bundles:
        if "krcert.or.kr" in bundle or "boho.or.kr" in bundle:
            assert "krcert.or.kr" in bundle and "boho.or.kr" in bundle
    assert bundles == [
        ["d1", "krcert.or.kr", "boho.or.kr"],
        ["krcert.or.kr", "boho.or.kr", "d2"],
        ["d2", "d1"],
    ]


def test_alias_group_starts_new_bundle_when_it_would_overflow():
    # d1,d2가 이미 2개 찬 번들에 alias 쌍(2개)이 더해지면 4개가 되어 넘치므로 새 번들로 간다.
    domains = ["d1", "d2", "krcert.or.kr", "boho.or.kr"]
    alias_groups = {"krcert.or.kr": ["boho.or.kr"]}

    bundles = build_bundles(domains, alias_groups)

    assert bundles == [
        ["d1", "d2"],
        ["d2", "krcert.or.kr", "boho.or.kr"],
        ["krcert.or.kr", "boho.or.kr", "d1"],
    ]


def test_original_domain_order_is_preserved_within_bundles():
    bundles = build_bundles(["z", "a", "m"], alias_groups={})
    assert bundles == [["z", "a", "m"]]  # 알파벳 정렬 같은 임의 재정렬을 하지 않는다
