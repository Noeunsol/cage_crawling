"""사이트별 전용 파서를 등록할 수 있는 레지스트리 (9.3절).

v1은 general_extractor만 쓴다. pilot 결과 특정 도메인의 실패율·노이즈가 높다고 확인되면
그때 그 도메인 전용 파서를 register()로 추가한다 — 미리 만들어두지 않는다 (YAGNI).
"""

from __future__ import annotations

from typing import Callable

from src.extraction.general_extractor import ExtractedContent, extract as general_extract

# (html, url, min_content_length, extraction_cfg) -> ExtractedContent
ParserFunc = Callable[[str, str, int, dict | None], ExtractedContent]

_REGISTRY: dict[str, ParserFunc] = {}


def register(domain: str, parser: ParserFunc) -> None:
    _REGISTRY[domain] = parser


def get_parser(domain: str) -> ParserFunc:
    return _REGISTRY.get(domain, general_extract)
