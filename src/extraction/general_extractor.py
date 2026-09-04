"""범용 본문 추출기. 사이트 전용 파서가 없는 곳은 전부 이걸로 처리한다.

HTML 태그·광고·메뉴·네비게이션 제거는 trafilatura가 담당한다 (이미 설치돼 있는 검증된 라이브러리 —
직접 BeautifulSoup 휴리스틱을 새로 짤 필요가 없다).
"""

from __future__ import annotations

from dataclasses import dataclass

import trafilatura

from src.extraction.cleaner import clean_article_text, title_from_html_tag


class ExtractionError(Exception):
    """reason은 extraction_empty(본문 자체를 못 찾음) / extraction_too_short(짧아서 탈락) 둘 중 하나.

    2026-08-31: 예전엔 "extraction_failed" 하나였는데, 원인 성격이 달라 retry_policy.yaml에서
    다른 재시도 정책을 쓴다(after_parser_update vs immediate) — src/utils/retry_policy_helpers.py.
    """

    def __init__(self, reason: str = "extraction_too_short"):
        self.reason = reason
        super().__init__(reason)


@dataclass
class ExtractedContent:
    title: str
    content: str
    published_date: str | None   # YYYY-MM-DD 또는 None


def build_or_raise(title: str, content: str, min_content_length: int, published_date: str | None) -> ExtractedContent:
    """제목이 없거나 본문이 min_content_length 미만이면 실패로 처리한다 (사이트 전용 파서 공용).

    general_extractor.extract()와 cook82/dcinside/ruliweb/instiz/kin_parser가 전부 이 기준으로
    성공/실패를 판단해서, 기준 자체가 바뀌면 여기 한 곳만 고치면 되게 모아뒀다.
    """
    if not title or len(content) < min_content_length:
        raise ExtractionError("extraction_too_short")
    return ExtractedContent(title=title, content=content, published_date=published_date)


def extract(
    html: str, url: str, min_content_length: int, extraction_cfg: dict | None = None,
) -> ExtractedContent:
    """제목이 없거나 본문이 min_content_length보다 짧으면 실패로 처리한다.

    extraction_cfg(configs/extraction.yaml)의 trafilatura 옵션으로 댓글·관련기사 등 본문이
    아닌 부분을 뺄지 조정할 수 있다. 안 넘기면 "본문만" 기본값을 그대로 쓴다.
    """
    trafilatura_cfg = (extraction_cfg or {}).get("trafilatura", {})
    # bare_extraction()으로 dict를 바로 받는다 — output_format="json" + json.loads는 본문에
    # 백슬래시가 섞이면("\초성" 이모티콘, 수식, 윈도 경로 등) trafilatura가 만든 JSON 문자열
    # 자체가 깨져서 JSONDecodeError가 난다(2026-08-26 실측). dict로 받으면 이 왕복이 아예 없다.
    document = trafilatura.bare_extraction(
        html, url=url, with_metadata=True,
        include_comments=trafilatura_cfg.get("include_comments", False),
        favor_precision=trafilatura_cfg.get("favor_precision", True),
        # date_extraction_params.original_date: 내부적으로 htmldate를 쓰는데 기본값(False)은
        # 페이지에서 찾은 "가장 최근" 날짜(최종수정일·댓글·관련기사 등)를 돌려준다 — 원문 게시일이
        # 아니라서 기간 필터에서 엉뚱하게 date_out_of_range로 빠지는 원인이 된다.
        date_extraction_params={"original_date": True},
    )
    if document is None:
        raise ExtractionError("extraction_empty")

    data = document.as_dict()
    title = (data.get("title") or "").strip() or title_from_html_tag(html)
    content = clean_article_text(
        data.get("text") or "",
        (extraction_cfg or {}).get("cleaning", {}),
    )

    return build_or_raise(title, content, min_content_length, data.get("date"))
