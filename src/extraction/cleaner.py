"""범용 추출기가 남긴 기사 꼬리말과 중복 문단을 정리한다.

사이트 전용 파서(cook82/dcinside/ruliweb/instiz/kin_parser)가 공통으로 쓰는 자잘한 헬퍼도
여기 모아둔다 — 다섯 파일에 거의 그대로 복붙돼 있던 걸 한 곳으로 뽑았다(2026-08-27).
"""

from __future__ import annotations

import re

from src.utils.text import normalize_whitespace

_NOISE_TAG_SELECTOR = "script, style, template, iframe, img"

# 기자명+이메일(예: "강지원 기자 jiwon.kang@sidae.com")은 거의 모든 한국 뉴스 매체가 기사
# 맨 끝에 붙이는 고정 형식이라, 매체별 문구(trailing_section_markers)를 따로 등록하지 않아도
# 이 줄부터는 바이라인 이후 영역(관련기사 목록 등)으로 보고 잘라낼 수 있다.
_REPORTER_BYLINE = re.compile(r"[가-힣]{2,4}\s*기자\s+[\w.+-]+@[\w.-]+\.\w+")


def strip_noise_tags(element) -> None:
    """본문 컨테이너 안의 스크립트/스타일/템플릿/iframe/이미지 태그를 제거한다."""
    for tag in element.select(_NOISE_TAG_SELECTOR):
        tag.decompose()


def title_with_meta_fallback(title_el, title_meta) -> str:
    """CSS로 찾은 제목 요소가 없으면 og:title 메타 태그 내용으로 대체한다."""
    if title_el is not None:
        return title_el.get_text(strip=True)
    if title_meta is not None:
        return (title_meta.get("content") or "").strip()
    return ""


def extract_dotted_date(text: str, sep: str = ".") -> str | None:
    """"2026.08.27" / "2026-08-27" 처럼 구분자로 이어진 날짜를 찾아 YYYY-MM-DD로 돌려준다."""
    pattern = re.compile(rf"(\d{{4}}){re.escape(sep)}(\d{{2}}){re.escape(sep)}(\d{{2}})")
    match = pattern.search(text)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else None


def clean_article_text(text: str, cleaning_cfg: dict) -> str:
    """기사 순서는 유지하면서 명확한 꼬리말과 반복 문단만 제거한다.

    꼬리말 표시는 사이트마다 다르므로 ``configs/extraction.yaml``에서 관리한다.
    반복 제거는 같은 문단이 완전히 일치할 때만 적용해 본문을 과하게 줄이지 않는다.
    """
    lines = normalize_whitespace(text).splitlines()

    # 일부 사이트(Q&A 플랫폼 등)는 트라필라투라가 상단 네비게이션 메뉴("홈", "토픽", "멤버십" 같은
    # 한 단어짜리 항목)까지 본문으로 끌고 온다. 메뉴 항목은 짧고, 실제 기사 문단은 훨씬 길다는
    # 점으로 구분한다 — 앞에서부터 짧은 줄이 연달아 나오는 동안만 잘라내고, 첫 "긴" 줄을 만나면 멈춘다.
    # 길이 기반 휴리스틱이라 아주 짧은 첫 문장으로 시작하는 기사는 잘못 잘릴 수 있음 —
    # 그 블라스트 반경을 줄이려고 최대 leading_short_line_max_count줄까지만 잘라내도록 막아둔다
    # (기본 5줄 — 실제 메뉴 노이즈는 보통 이보다 훨씬 짧다). 오탐이 계속 나오면 이 로직 자체를 재검토할 것.
    max_leading_len = cleaning_cfg.get("leading_short_line_max_length", 0)
    max_leading_count = cleaning_cfg.get("leading_short_line_max_count", 5)
    if max_leading_len:
        cut = 0
        for line in lines:
            if cut >= max_leading_count:
                break
            stripped = line.strip()
            if stripped and len(stripped) > max_leading_len:
                break
            cut += 1
        lines = lines[cut:]

    trailing_markers = tuple(cleaning_cfg.get("trailing_section_markers", []))
    bullet_prefixes = tuple(cleaning_cfg.get("trailing_bullet_prefixes", []))
    bullet_min_run = cleaning_cfg.get("trailing_bullet_min_run", 0)
    # "저작권자"/"무단전재" 같은 단어는 이 프로젝트가 수집하는 저작권 침해 사건 기사 본문
    # 한복판에도 정당하게 나올 수 있다 — 마커/바이라인/불릿 판정 전부를 "문서 끝 부근"으로
    # 한정해서, 우연히 걸린 단어 하나 때문에 뒤따르는 진짜 본문이 통째로 잘리지 않게 한다
    # (leading_short_line_*과 대칭되는 안전장치. 0이면 문서 전체를 대상으로 한다).
    search_window = cleaning_cfg.get("trailing_search_window", 0)

    def _bullet_run_starts_at(start: int) -> bool:
        # 화살표·불릿 기호로 시작하는 짧은 줄이 연달아 나오는 위젯 생김새는 사이트가 달라도
        # 공통적이다 — 우연한 목록(인용문 등)까지 지우지 않도록 최소 연속 개수를 요구한다.
        found = 0
        i = start
        while i < len(lines) and found < bullet_min_run:
            stripped = lines[i].strip()
            if not stripped:
                i += 1
                continue
            if not stripped.startswith(bullet_prefixes):
                return False
            found += 1
            i += 1
        return found >= bullet_min_run

    for index, line in enumerate(lines):
        if search_window and index < len(lines) - search_window:
            continue

        stripped = line.strip()
        is_bullet_widget = (
            bullet_prefixes and bullet_min_run
            and stripped.startswith(bullet_prefixes) and _bullet_run_starts_at(index)
        )
        if any(marker in stripped for marker in trailing_markers) or _REPORTER_BYLINE.search(stripped) or is_bullet_widget:
            cut_at = index
            # 위젯 제목("관련기사", "이 기사가 좋으셨다면" 등)이 불릿 목록 바로 앞의 짧은 줄로
            # 붙어 있으면 그것도 같이 지운다.
            if is_bullet_widget and cut_at > 0:
                heading = lines[cut_at - 1].strip()
                if heading and not heading.startswith(bullet_prefixes) and len(heading) <= 20:
                    cut_at -= 1
            lines = lines[:cut_at]
            break

    if cleaning_cfg.get("remove_duplicate_paragraphs", False):
        minimum_length = cleaning_cfg.get("duplicate_paragraph_min_length", 0)
        seen: set[str] = set()
        unique_lines: list[str] = []

        for line in lines:
            paragraph = line.strip()
            if len(paragraph) >= minimum_length:
                if paragraph in seen:
                    continue
                seen.add(paragraph)
            unique_lines.append(line)
        lines = unique_lines

    return normalize_whitespace("\n".join(lines))
