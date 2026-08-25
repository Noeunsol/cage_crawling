"""범용 본문 추출기 (9.2~9.4절). 사이트 전용 파서가 없는 곳은 전부 이걸로 처리한다.

HTML 태그·광고·메뉴·네비게이션 제거는 trafilatura가 담당한다 (이미 설치돼 있는 검증된 라이브러리 —
직접 BeautifulSoup 휴리스틱을 새로 짤 필요가 없다).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import trafilatura

from src.utils.text import normalize_whitespace


class ExtractionError(Exception):
    def __init__(self, reason: str = "extraction_failed"):
        self.reason = reason
        super().__init__(reason)


@dataclass
class ExtractedContent:
    title: str
    content: str
    published_date: str | None   # YYYY-MM-DD 또는 None (8.3절: 못 찾으면 저장하지 않는다)


def extract(
    html: str, url: str, min_content_length: int, extraction_cfg: dict | None = None,
) -> ExtractedContent:
    """제목이 없거나 본문이 min_content_length보다 짧으면 실패로 처리한다 (9.4, 9.5절).

    extraction_cfg(configs/extraction.yaml)의 trafilatura 옵션으로 댓글·관련기사 등 본문이
    아닌 부분을 뺄지 조정할 수 있다. 안 넘기면 "본문만" 기본값을 그대로 쓴다.
    """
    trafilatura_cfg = (extraction_cfg or {}).get("trafilatura", {})
    raw = trafilatura.extract(
        html, url=url, output_format="json", with_metadata=True,
        include_comments=trafilatura_cfg.get("include_comments", False),
        favor_precision=trafilatura_cfg.get("favor_precision", True),
        # date_extraction_params.original_date: 내부적으로 htmldate를 쓰는데 기본값(False)은
        # 페이지에서 찾은 "가장 최근" 날짜(최종수정일·댓글·관련기사 등)를 돌려준다 — 원문 게시일이
        # 아니라서 기간 필터에서 엉뚱하게 date_out_of_range로 빠지는 원인이 된다.
        date_extraction_params={"original_date": True},
    )
    if raw is None:
        raise ExtractionError("extraction_failed")

    data = json.loads(raw)
    title = (data.get("title") or "").strip()
    content = normalize_whitespace(data.get("text") or "")

    if not title or len(content) < min_content_length:
        raise ExtractionError("extraction_failed")

    return ExtractedContent(title=title, content=content, published_date=data.get("date"))
