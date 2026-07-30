"""seed_url — 고정 seed URL을 그대로 후보로 만든다."""
from __future__ import annotations

from .base import make_candidate


def discover(task, subtype, registry) -> list:
    return [make_candidate(registry, u, task, "seed_url", subtype) for u in task.seed_urls]
