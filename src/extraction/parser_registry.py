"""사이트별 전용 파서·source_category를 등록할 수 있는 레지스트리 (9.3절).

v1은 general_extractor만 쓴다. pilot 결과 특정 도메인의 실패율·노이즈가 높다고 확인되면
그때 그 도메인 전용 파서를 register()로 추가한다 — 미리 만들어두지 않는다 (YAGNI).

도메인 하나에 대한 "전용 파서가 있는지"와 "어떤 카테고리인지"를 예전엔 별개의 두 dict로
관리했는데, 도메인이 늘면서 서로 드리프트하기 쉬웠다(2026-08-27) — 한 도메인의 정보를
_DomainConfig 하나로 모았다. 파서는 정확히 일치하는 도메인에만, 카테고리는 서브도메인까지
적용된다는 기존 매칭 규칙은 그대로 유지한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from src.extraction.cook82_parser import parse as cook82_parse
from src.extraction.dcinside_parser import parse as dcinside_parse
from src.extraction.general_extractor import ExtractedContent, extract as general_extract
from src.extraction.instiz_parser import parse as instiz_parse
from src.extraction.kin_parser import parse as kin_parse
from src.extraction.ruliweb_parser import parse as ruliweb_parse

# (html, url, min_content_length, extraction_cfg) -> ExtractedContent
ParserFunc = Callable[[str, str, int, dict | None], ExtractedContent]


@dataclass
class _DomainConfig:
    parser: ParserFunc | None = None   # None이면 get_parser()가 general_extract로 대체한다
    category: str | None = None        # None이면 get_source_category()가 그냥 못 찾은 걸로 처리한다


_DOMAINS: dict[str, _DomainConfig] = {
    # bbs.ruliweb.com: 범용 추출기가 옆 게시판 목록 표를 본문으로 잘못 집는 실패율이 높아
    # 전용 파서로 교체 (2026-08-26 실측).
    "bbs.ruliweb.com": _DomainConfig(parser=ruliweb_parse),
    "ruliweb.com": _DomainConfig(category="community"),
    # 82cook.com: 댓글 스레드가 원글보다 훨씬 길어서 범용 추출기가 댓글을 본문으로 집고
    # 제목도 못 찾는 실패가 확인됨 (2026-08-26 실측). www 유무 둘 다 등록.
    "82cook.com": _DomainConfig(parser=cook82_parse, category="community"),
    "www.82cook.com": _DomainConfig(parser=cook82_parse),
    # gall.dcinside.com: 범용 추출기가 갤러리 목록(다른 글 제목 등)을 본문으로 잘못 집는
    # 실패가 확인됨 (2026-08-26 실측).
    "gall.dcinside.com": _DomainConfig(parser=dcinside_parse, category="community"),
    # 목록·카테고리 페이지를 본문으로 오인하지 않고 상세 질문/게시글만 받는다.
    "kin.naver.com": _DomainConfig(parser=kin_parse, category="qna"),
    "www.instiz.net": _DomainConfig(parser=instiz_parse),
    "instiz.net": _DomainConfig(parser=instiz_parse, category="community"),
    "bobaedream.co.kr": _DomainConfig(category="community"),
    "coinpan.com": _DomainConfig(category="community"),
    "inven.co.kr": _DomainConfig(category="community"),
    "mlbpark.donga.com": _DomainConfig(category="community"),
    "okky.kr": _DomainConfig(category="community"),
    "paxnet.co.kr": _DomainConfig(category="community"),
    "ppomppu.co.kr": _DomainConfig(category="community"),
}


def register(domain: str, parser: ParserFunc) -> None:
    _DOMAINS.setdefault(domain, _DomainConfig()).parser = parser


def get_parser(domain: str) -> ParserFunc:
    entry = _DOMAINS.get(domain)
    return entry.parser if entry and entry.parser else general_extract


def get_source_category(domain: str) -> str | None:
    return next(
        (entry.category for base, entry in _DOMAINS.items()
         if entry.category and (domain == base or domain.endswith(f".{base}"))),
        None,
    )
