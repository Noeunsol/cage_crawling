"""추출기 공통 — ExtractionOutcome, site parser 계약, HTML 헬퍼."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from src.common.schema import ContentRecord

_HANGUL = re.compile(r"[가-힣]")
_DATE = re.compile(r"\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}")


@dataclass
class ExtractionOutcome:
    record: ContentRecord | None
    tried: list = field(default_factory=list)
    reason: str | None = None


# site parser는 상속이 아니라 덕 타이핑 계약이다: `name` 속성 + `extract(c, site, html)`.
# ExtractorRouter._ladder가 SITE_PARSER_REGISTRY 이름으로 rung을 고른다.


# ── HTML 헬퍼 ──

def harvest_strings(obj) -> list[str]:
    """중첩 JSON에서 문자열 값을 모두 수집 (지식인 __NEXT_DATA__용 heuristic)."""
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(harvest_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(harvest_strings(v))
    return out


def title_of(soup: BeautifulSoup) -> str | None:
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return None


def title_of_html(html: str) -> str | None:
    return title_of(BeautifulSoup(html, "lxml"))


def first_date(text: str) -> str | None:
    m = _DATE.search(text or "")
    if not m:
        return None
    return re.sub(r"[.\-/]", "-", m.group(0))
