"""게시판 목록 discovery. 현재 실제 adapter는 DCInside만 지원한다."""
from __future__ import annotations

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
