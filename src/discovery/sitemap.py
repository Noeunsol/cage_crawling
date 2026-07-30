"""sitemap — task.seed_urls의 sitemap.xml에서 <loc> 링크를 추출."""
from __future__ import annotations

from .base import feed_links, make_candidate


def discover(task, subtype, registry) -> list:
    out = []
    for feed in task.seed_urls:
        for u in feed_links("sitemap", feed):
            if u and u.startswith("http"):
                out.append(make_candidate(registry, u, task, "sitemap", subtype))
    return out
