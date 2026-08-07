"""trend 모드 후보 선별·윈도우·본문 링크 헬퍼 (버킷 배분, RSS 카테고리 배분, 날짜 윈도우)."""
from __future__ import annotations

import datetime as _dt
import hashlib
import math
import re
from collections import defaultdict
from urllib.parse import urlparse

from ..schema import UrlCandidate

_URL_RE = re.compile(r'https?://[^\s"\'<>)\]}]+')
_SKIP_LINK_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".mp3", ".pdf")


def _allocate_buckets(cands: list, target: int, ratios: dict) -> list:
    """버킷별 비중(sampling_ratio)에 따라 target 만큼 배분. 미달 시 leftover로 보충."""
    if not target:
        return cands
    by: defaultdict = defaultdict(list)
    for c in cands:
        by[c.meta.get("bucket", "latest")].append(c)
    picked, leftover = [], []
    for bucket, items in by.items():
        quota = round(target * ratios.get(bucket, 0)) if ratios else len(items)
        picked.extend(items[:quota])
        leftover.extend(items[quota:])
    if len(picked) < target:
        picked.extend(leftover[: target - len(picked)])
    return picked[:target]


def _engagement(cand) -> float:
    meta = cand.meta
    return (
        float(meta.get("comment_count") or 0) * 3
        + float(meta.get("like_count") or 0) * 2
        + math.log1p(float(meta.get("view_count") or 0))
    )


def _mixed_pick(cands: list, target: int, engagement_ratio: float, seed: str) -> list:
    """반응 상위 + URL 해시 분산 샘플. 같은 입력은 항상 같은 결과를 만든다."""
    if target <= 0 or not cands:
        return []
    target = min(target, len(cands))
    hot_n = min(target, round(target * engagement_ratio))
    hot = sorted(cands, key=lambda c: (-_engagement(c), c.dedup_key()))[:hot_n]
    used = {c.dedup_key() for c in hot}
    rest = [c for c in cands if c.dedup_key() not in used]
    rest.sort(key=lambda c: hashlib.sha1(
        f"{seed}:{c.dedup_key()}".encode("utf-8")
    ).hexdigest())
    return hot + rest[: target - len(hot)]


def _topic_pick(cands: list, target: int, ratios: dict,
                engagement_ratio: float, seed: str) -> list:
    """주제 quota를 먼저 채우고 부족분은 남은 전체 후보에서 보충한다."""
    by: defaultdict = defaultdict(list)
    for cand in cands:
        by[cand.meta.get("bucket", "latest")].append(cand)
    picked = []
    for bucket, items in by.items():
        quota = round(target * float(ratios.get(bucket, 0))) if ratios else len(items)
        picked.extend(_mixed_pick(items, quota, engagement_ratio, f"{seed}:{bucket}"))
    used = {c.dedup_key() for c in picked}
    leftover = [c for c in cands if c.dedup_key() not in used]
    picked.extend(_mixed_pick(
        leftover, target - len(picked), engagement_ratio, f"{seed}:leftover"
    ))
    return picked[:target]


def _sample_candidates_by_time(cands: list, lookback_days: int, daily_quota: int,
                               ratios: dict, timezone, time_bucket_hours: int = 4,
                               engagement_ratio: float = 0.3,
                               absolute_max: int = 1000) -> list:
    """날짜별 quota를 시간대에 균등 배분하고 부족분은 날짜 안→날짜 간 순으로 보충한다."""
    if daily_quota <= 0:
        return []
    today = _dt.datetime.now(timezone).date()
    dates = [today - _dt.timedelta(days=i) for i in range(lookback_days)]
    by_day: defaultdict = defaultdict(list)
    unknown = []
    for cand in cands:
        published = _parse_datetime(cand.published_at_hint, timezone)
        if published and published.date() in dates:
            by_day[published.date()].append(cand)
        elif published is None:
            unknown.append(cand)

    bucket_count = max(1, 24 // max(1, time_bucket_hours))
    picked = []
    for day in dates:
        day_items = by_day[day]
        by_hour: defaultdict = defaultdict(list)
        for cand in day_items:
            published = _parse_datetime(cand.published_at_hint, timezone)
            by_hour[published.hour // time_bucket_hours].append(cand)
        day_picked = []
        for slot in range(bucket_count):
            quota = daily_quota // bucket_count + (slot < daily_quota % bucket_count)
            day_picked.extend(_topic_pick(
                by_hour[slot], quota, ratios, engagement_ratio, f"{day}:{slot}"
            ))
        used = {c.dedup_key() for c in day_picked}
        leftover = [c for c in day_items if c.dedup_key() not in used]
        day_picked.extend(_topic_pick(
            leftover, daily_quota - len(day_picked), ratios, engagement_ratio, f"{day}:day"
        ))
        picked.extend(day_picked)

    target = min(absolute_max, daily_quota * lookback_days, len(cands))
    used = {c.dedup_key() for c in picked}
    surplus = [c for c in cands if c.dedup_key() not in used and c not in unknown]
    surplus.extend(c for c in unknown if c.dedup_key() not in used)
    picked.extend(_topic_pick(
        surplus, target - len(picked), ratios, engagement_ratio, "cross-day"
    ))
    return picked[:target]


def _append_unique_candidates(cands, selected, seen, scan_cap):
    """목록 후보를 URL 중복 없이 안전 상한까지 추가한다."""
    for cand in cands:
        key = cand.dedup_key()
        if key in seen:
            continue
        if len(selected) >= scan_cap:
            break
        seen.add(key)
        selected.append(cand)


def _round_robin_candidates(by_source: dict[str, list]) -> list:
    """소스별 후보를 한 건씩 번갈아 배치한다."""
    groups = list(by_source.values())
    return [group[i] for i in range(max(map(len, groups), default=0))
            for group in groups if i < len(group)]


def _target_stats(available, scan_cap):
    stats = {
        "available": available,
        "scanned": 0,
        "title_keep": 0,
        "prefilter_discard": 0,
        "content_discard": 0,
        "extraction_failed": 0,
        "discard": 0,
        "duplicate": 0,
        "accepted": 0,
        "already_processed": 0,
        "scan_cap": scan_cap,
        "cap_reached": available >= scan_cap,
    }
    return stats


def _allocate_news_categories(cands: list, target: int, ratios: dict) -> list:
    """URL 중복을 제거하고 RSS category별 목표 비율로 후보를 배분한다."""
    unique = list({c.dedup_key(): c for c in cands}.values())
    if not target:
        return unique
    by: defaultdict = defaultdict(list)
    for cand in unique:
        by[cand.meta.get("category_name", "")].append(cand)
    picked, leftover = [], []
    for category, items in by.items():
        quota = round(target * ratios.get(category, 0)) if ratios else len(items)
        picked.extend(items[:quota])
        leftover.extend(items[quota:])
    if len(picked) < target:
        picked.extend(leftover[:target - len(picked)])
    return picked[:target]


def _extract_links(text: str | None, comments: list | None) -> list[tuple[str, str]]:
    """본문+댓글 링크와 출처를 추출. 이미지·중복은 제외(동영상은 후보 단계에서 discard 기록)."""
    out, seen = [], set()
    chunks = [(text or "", "body")] + [(comment, "comment") for comment in (comments or [])]
    for chunk, link_source in chunks:
        for u in _URL_RE.findall(chunk):
            u = u.rstrip(".,)]}\"'")
            if u.lower().endswith(_SKIP_LINK_EXT) or u in seen:
                continue
            seen.add(u)
            out.append((u, link_source))
    return out


def _link_candidate(url: str, registry, parent_source: str,
                    parent_source_url: str, link_source: str) -> UrlCandidate:
    """본문 링크 → 후속 UrlCandidate(depth 1). 도메인으로 site_type 추론."""
    domain = urlparse(url).netloc.lower()
    info = registry.lookup(domain)
    st = "news" if info.site_type == "news" else (
        "community" if info.site_type in ("community", "dynamic") else info.site_type)
    cand = UrlCandidate(url, domain, f"link_from:{parent_source}", "in_body_link", "", "",
                        canonical_url=url, site_name=info.site_name, site_type=info.site_type,
                        collection_type="link", discovery_method="in_body_link",
                        parent_source_url=parent_source_url, link_source=link_source,
                        is_supplementary=True)
    cand.meta = {"source": info.site_name or domain, "source_type": st,
                 "board_name": "in_body_link", "bucket": "link", "is_trending": False, "depth": 1}
    return cand


def _parse_datetime(value: str | None, timezone) -> _dt.datetime | None:
    if not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed.astimezone(timezone)


def _candidate_in_window(cand, cutoff: _dt.datetime) -> bool:
    """날짜 미상 후보는 상세 메타데이터로 재확인하기 위해 포함한다."""
    published = _parse_datetime(cand.published_at_hint, cutoff.tzinfo)
    return published is None or published >= cutoff


def _published_in_window(value: str | None, cutoff: _dt.datetime) -> bool:
    published = _parse_datetime(value, cutoff.tzinfo)
    return published is None or published >= cutoff


def _days_old(published_at: str | None, today) -> int | None:
    if not published_at:
        return None
    try:
        d = _dt.date.fromisoformat(published_at[:10])
    except ValueError:
        return None
    return (today - d).days
