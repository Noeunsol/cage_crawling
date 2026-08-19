"""게시판 목록 discovery. 현재 실제 adapter는 DCInside만 지원한다."""
from __future__ import annotations

import datetime as _dt
import logging
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..schema import UrlCandidate
from .base import make_candidate

log = logging.getLogger(__name__)


def _integer(text: str) -> int:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else 0


def _page_url(url: str, page: int) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _prefilter_score(title: str, comments: int, subtype, board: dict) -> float:
    text = title.lower()
    terms = list(subtype.keywords) + list(subtype.target_harm_signals)
    signals = list(board.get("title_signals", []))
    signals += board.get("signals_by_subtype", {}).get(subtype.name, [])
    term_score = min(sum(str(x).lower() in text for x in set(terms)) * 0.25, 0.5)
    signal_score = min(sum(str(x).lower() in text for x in set(signals)) * 0.2, 0.4)
    if board.get("require_title_signal") and not (term_score or signal_score):
        return 0.0
    comment_score = 0.15 if comments >= 10 else 0.1 if comments >= 3 else 0.0
    return round(min(1.0, float(board.get("weight", 0.0)) + term_score
                     + signal_score + comment_score), 3)


def parse_dcinside_list(html: str, list_url: str, task, subtype, registry,
                        exclude_notice: bool = True, board: dict | None = None) -> list:
    """DCInside 목록 HTML을 UrlCandidate로 변환한다. 네트워크 호출 없는 순수 parser."""
    board = board or {}
    soup = BeautifulSoup(html, "lxml")
    out = []
    for row in soup.select("tr.ub-content[data-no]"):
        number = row.select_one(".gall_num")
        # 일반 갤러리는 icon_txt, 실베는 icon_btimebest를 사용한다.
        # 아이콘 이름 대신 숫자 게시물 번호로 공지·설문·광고를 제외한다.
        if exclude_notice and (not number or not number.get_text(strip=True).isdigit()):
            continue
        link = row.select_one("td.gall_tit > a[href*='/board/view']")
        if not link:
            continue
        title = link.get_text(" ", strip=True)
        reply = row.select_one(".reply_num")
        if reply:
            title = title.removesuffix(reply.get_text(" ", strip=True)).strip()
        comment_count = _integer(reply.get_text()) if reply else 0
        prefilter_score = _prefilter_score(title, comment_count, subtype, board)
        if prefilter_score < float(board.get("min_prefilter_score", 0.0)):
            continue
        candidate = make_candidate(
            registry, urljoin(list_url, link.get("href")), task, "board_list", subtype, title
        )
        candidate.search_query = f"board:{board.get('name', 'dcinside')}"
        candidate.initial_score = candidate.score = prefilter_score
        date = row.select_one(".gall_date")
        candidate.published_at_hint = date.get("title") if date else None
        candidate.snippet = (
            f"comments={comment_count};"
            f"views={_integer(row.select_one('.gall_count').get_text()) if row.select_one('.gall_count') else 0}"
        )
        out.append(candidate)
    return out


def discover(task, subtype, registry, fetcher) -> list:
    out = []
    cap = 20
    for site_name in subtype.priority_sites:
        site = registry.site_for(site_name)
        cfg = site.board_discovery if site else {}
        if not cfg.get("enabled") or cfg.get("adapter") != "dcinside":
            continue
        fetcher.per_domain_delay = max(
            fetcher.per_domain_delay, float(cfg.get("request_delay", 2.0))
        )
        cap = int(cfg.get("max_candidates_per_board", cap))
        for board in cfg.get("boards", []):
            allowed_subtypes = board.get("subtypes", ["*"])
            if "*" not in allowed_subtypes and subtype.name not in allowed_subtypes:
                continue
            list_url = board.get("list_url")
            if not list_url:
                continue
            board_candidates = []
            for page in range(1, int(cfg.get("max_pages", 1)) + 1):
                html = fetcher.fetch(_page_url(list_url, page))
                if not html:
                    log.info("DCInside 목록 fetch 실패 board=%s page=%s", board.get("name"), page)
                    break
                board_candidates.extend(parse_dcinside_list(
                    html, list_url, task, subtype, registry,
                    bool(cfg.get("exclude_notice", True)), board,
                ))
            board_candidates.sort(key=lambda c: c.initial_score, reverse=True)
            out.extend(board_candidates[:int(board.get("max_candidates", cap))])
    return sorted(out, key=lambda c: c.initial_score, reverse=True)[:cap]


# ── 트렌드 모드 (taxonomy 무관, 샘플링 버킷 기반) ──
def _gallery_url(gallery: dict) -> str:
    """config gallery → 목록 URL. list_url 우선, 없으면 id로 조립(mgallery 지원)."""
    if gallery.get("list_url"):
        return gallery["list_url"]
    gid = gallery["id"]
    base = "mgallery/board/lists" if gallery.get("minor") else "board/lists"
    return f"https://gall.dcinside.com/{base}/?id={gid}"


def parse_dcinside_trend(html, list_url, registry, bucket, board_name,
                         exclude_notice=True, is_trending=None) -> list:
    """DCInside 목록을 taxonomy 프리필터 없이 UrlCandidate로 변환. 조회/댓글/추천수를 meta에 싣는다.

    bucket은 주제 태그(gender_conflict/issue_or_politics/...)이고, is_trending은 별도 플래그다.
    is_trending 미지정 시 하위호환으로 bucket=="trending"에서 True.
    """
    trending = (bucket == "trending") if is_trending is None else bool(is_trending)
    soup = BeautifulSoup(html, "lxml")
    out = []
    for row in soup.select("tr.ub-content[data-no]"):
        number = row.select_one(".gall_num")
        if exclude_notice and (not number or not number.get_text(strip=True).isdigit()):
            continue
        link = row.select_one("td.gall_tit > a[href*='/board/view']")
        if not link:
            continue
        title = link.get_text(" ", strip=True)
        reply = row.select_one(".reply_num")
        if reply:
            title = title.removesuffix(reply.get_text(" ", strip=True)).strip()
        comment_count = _integer(reply.get_text()) if reply else 0
        views = _integer(row.select_one(".gall_count").get_text()) if row.select_one(".gall_count") else 0
        recommend = _integer(row.select_one(".gall_recommend").get_text()) if row.select_one(".gall_recommend") else 0
        url = urljoin(list_url, link.get("href"))
        domain = urlparse(url).netloc.lower()
        info = registry.lookup(domain)
        cand = UrlCandidate(
            url, domain, f"trend:{board_name}", "board_list", "", "",
            title=title, canonical_url=url, site_name=info.site_name, site_type=info.site_type,
            collection_type="raw_expression", discovery_method="board_list",
        )
        date = row.select_one(".gall_date")
        cand.published_at_hint = date.get("title") if date else None
        cand.snippet = f"comments={comment_count};views={views}"
        cand.score = cand.initial_score = float(comment_count)
        cand.meta = {
            "source": "dcinside", "source_type": "community", "board_name": board_name,
            "bucket": bucket, "is_trending": trending,
            "view_count": views, "comment_count": comment_count, "like_count": recommend,
        }
        out.append(cand)
    return out


def discover_dcinside_trend(galleries, registry, fetcher, max_pages: int = 1,
                            exclude_notice: bool = True, start_page: int = 1) -> list:
    """config의 갤러리 목록을 순회해 버킷 태깅된 후보를 수집한다(추출/분류 이전)."""
    out = []
    for g in galleries:
        list_url = _gallery_url(g)
        bucket = g.get("bucket", "latest")
        trending = g.get("trending")   # 명시 플래그(dcbest=실베 등). 없으면 bucket으로 하위호환
        board_name = g.get("name") or g.get("id")
        cands = []
        for page in range(start_page, start_page + max_pages):
            html = fetcher.fetch(_page_url(list_url, page))
            if not html:
                log.info("DCInside 트렌드 목록 fetch 실패 gallery=%s page=%s", board_name, page)
                break
            cands.extend(parse_dcinside_trend(
                html, list_url, registry, bucket, board_name, exclude_notice, trending))
        if g.get("sort") == "comments" or bucket == "high_comment":
            cands.sort(key=lambda c: c.meta["comment_count"], reverse=True)
        out.extend(cands)
    return out


# ── 일간베스트 (정적 목록) ──
def parse_ilbe_trend(html, list_url, registry, bucket, board_name) -> list:
    """일베 목록 → UrlCandidate. 구조는 li > a.subject + span.date/view/comment."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for row in soup.find_all("li"):
        if "notice-line" in (row.get("class") or []):   # 공지 제외
            continue
        link = row.select_one('a.subject[href*="/view/"]')
        if not link:
            continue
        views = _integer(row.select_one("span.view").get_text()) if row.select_one("span.view") else 0
        comments = _integer(row.select_one("span.comment").get_text()) if row.select_one("span.comment") else 0
        url = urljoin(list_url, link.get("href"))
        domain = urlparse(url).netloc.lower()
        info = registry.lookup(domain)
        cand = UrlCandidate(
            url, domain, f"trend:{board_name}", "board_list", "", "",
            title=link.get_text(" ", strip=True), canonical_url=url,
            site_name=info.site_name, site_type=info.site_type,
            collection_type="raw_expression", discovery_method="board_list",
        )
        date = row.select_one("span.date")
        # 당일 글은 "11:04:11", 이전 글은 "2026.08.13" 형태로 내려온다.
        cand.published_at_hint = _ilbe_datetime(date.get_text(strip=True)) if date else None
        cand.snippet = f"comments={comments};views={views}"
        cand.score = cand.initial_score = float(comments)
        cand.meta = {
            "source": "ilbe", "source_type": "community", "board_name": board_name,
            "bucket": bucket, "is_trending": False,
            "view_count": views, "comment_count": comments, "like_count": 0,
        }
        out.append(cand)
    return out


def _ilbe_datetime(text: str) -> str | None:
    """목록의 시각 표기를 ISO로. 시:분:초만 있으면 오늘 날짜를 붙인다."""
    text = (text or "").strip()
    if re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", text):
        return f"{_dt.date.today().isoformat()}T{text if len(text) > 5 else text + ':00'}"
    m = re.fullmatch(r"(\d{4})[.\-/](\d{2})[.\-/](\d{2})", text)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T00:00:00" if m else None


def discover_ilbe_trend(boards, registry, fetcher, max_pages: int = 1, start_page: int = 1) -> list:
    out = []
    for b in boards:
        list_url = b.get("list_url") or f"https://www.ilbe.com/list/{b['id']}"
        name = b.get("name") or b.get("id")
        for page in range(start_page, start_page + max_pages):
            url = list_url if page == 1 else f"{list_url}?page={page}"
            html = fetcher.fetch(url)
            if not html:
                log.info("일베 목록 fetch 실패 board=%s page=%s", name, page)
                break
            out.extend(parse_ilbe_trend(html, list_url, registry, b.get("bucket", "latest"), name))
    return out


# ── 닥터나우 (정적 목록, 의료 상담 Q&A) ──
def parse_doctornow_trend(html, list_url, registry, bucket, board_name) -> list:
    """닥터나우 실시간 상담 목록 → UrlCandidate.

    CSS class가 styled-components 해시라 빌드마다 바뀐다. 태그 구조로만 파싱한다.
    목록 카드의 ``YYYY.MM.DD`` 표기를 published_at_hint로 보존한다.
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    seen = set()
    for article in soup.find_all("article"):
        link = article.find_parent("a") or article.select_one("a[href]")
        href = link.get("href") if link else None
        if not href or not re.search(r"/content/qna/\w+", href) or href in seen:
            continue
        seen.add(href)
        heading = article.find("h2")
        summary = article.find("p")
        dept = article.find("h3")
        url = urljoin(list_url, href)
        domain = urlparse(url).netloc.lower()
        info = registry.lookup(domain)
        cand = UrlCandidate(
            url, domain, f"trend:{board_name}", "board_list", "", "",
            title=heading.get_text(" ", strip=True) if heading else "",
            canonical_url=url, site_name=info.site_name, site_type=info.site_type,
            collection_type="qa_consulting", discovery_method="board_list",
        )
        date = re.search(r"\b(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b", article.get_text(" ", strip=True))
        cand.published_at_hint = (
            f"{date.group(1)}-{int(date.group(2)):02d}-{int(date.group(3)):02d}T00:00:00"
            if date else None
        )
        cand.snippet = summary.get_text(" ", strip=True)[:200] if summary else None
        cand.meta = {
            "source": "doctornow", "source_type": "qna", "board_name": board_name,
            "bucket": bucket, "topic": bucket, "is_trending": False,
            "category_name": dept.get_text(strip=True) if dept else "",
            "view_count": 0, "comment_count": 0, "like_count": 0,
        }
        out.append(cand)
    return out


def discover_doctornow_trend(boards, registry, fetcher, max_pages: int = 1, start_page: int = 1) -> list:
    out = []
    for b in boards:
        list_url = b.get("list_url") or "https://doctornow.co.kr/content/qna/realtime"
        name = b.get("name") or b.get("id", "doctornow")
        for page in range(start_page, start_page + max_pages):
            url = list_url if page == 1 else f"{list_url}?page={page}"
            html = fetcher.fetch(url)
            if not html:
                log.info("닥터나우 목록 fetch 실패 page=%s", page)
                break
            out.extend(parse_doctornow_trend(html, list_url, registry, b.get("bucket", "latest"), name))
    return out
