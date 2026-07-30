"""exa / github — AI·보안 문서 검색 provider (미구현 seam).

client 미구현이면 경고만 남기고 빈 결과. 실제 provider 붙이면 search_discover가 활성화.
"""
from __future__ import annotations

import logging

from ..search import get_client
from .base import search_discover

log = logging.getLogger(__name__)


def discover(method, task, subtype, registry, query_gen, budget, max_urls) -> list:
    if get_client(method) is None:
        log.info("%s provider 미구현 seam → skip", method)
        return []
    return search_discover(method, method, task, subtype, registry, query_gen,
                           budget, max_urls)
