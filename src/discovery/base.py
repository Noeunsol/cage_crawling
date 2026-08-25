"""discovery provider(Tavily/SerpAPI) 공통 결과 모델과 재요청 방지용 fingerprint (9.1, 10.1절).

검색 API는 URL을 찾는 용도일 뿐이다 (9.1절: "검색 결과 snippet을 최종 content로 저장하지 않는다").
그래서 DiscoveredResult에는 url/rank/relevance_score만 있고 본문·snippet 필드는 없다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date


@dataclass
class DiscoveredResult:
    url: str
    rank: int
    relevance_score: float | None = None


@dataclass
class DiscoveryResponse:
    results: list[DiscoveredResult]
    request_params: dict   # provider에 실제로 보낸 파라미터 (query_executions.request_params에 그대로 저장)
    usage: dict            # provider가 돌려준 usage/credit 정보


def build_fingerprint(
    provider: str, query_text: str, date_from: date, date_to: date, extra: dict | None = None
) -> str:
    """동일 provider·검색어·기간(·추가 조건)의 재요청을 막기 위한 지문 (10.1절).

    query_executions.request_fingerprint에 UNIQUE 제약이 걸려 있어, 이 값이 같으면
    storage 계층에서 자동으로 재호출을 막는다 (src/storage/repositories/query_executions.py).
    """
    payload = {
        "provider": provider,
        "query_text": query_text,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "extra": extra or {},
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
