"""URL/본문 중복 체크 (10.2, 10.3절). 실제로 저장할지 말지는 호출부(파이프라인, Phase 9~10)가 정한다."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.storage.repositories import contents as contents_repo
from src.utils import similarity


@dataclass
class DuplicateCheck:
    is_duplicate: bool
    reason: str | None = None            # "same_url" / "same_content_hash"
    existing_content_id: int | None = None


@dataclass
class NearDuplicateCheck:
    is_duplicate: bool                     # 현재 enforce 임계값 기준 판정
    matched_content_id: int | None = None  # is_duplicate 판정에 쓰인 상대 (없으면 best_* 참고)
    title_similarity: float = 0.0
    content_similarity: float = 0.0
    # 임계값을 넘겼는지와 무관하게 content_similarity가 가장 높았던 상대 — shadow mode 로깅용
    # (2026-08-31). 임계값 근처의 "아깝게 안 걸린" 사례까지 봐야 나중에 라벨링으로 기준을 잡을 수 있다.
    best_content_id: int | None = None
    best_title_similarity: float = 0.0
    best_content_similarity: float = 0.0


# 재게시/경미 수정 재업로드 판정 기준 (실측 기반 재조정 — src/utils/similarity.py 상단 주석 참고).
# 100쌍 정도 사람이 라벨링해서 확정하기 전까지는 잠정치 — 그래서 기본은 shadow mode다.
TITLE_SIMILARITY_THRESHOLD = 0.90
CONTENT_SIMILARITY_WITH_TITLE_MATCH = 0.35   # 제목도 비슷할 때는 본문 기준을 낮게 잡아도 됨
CONTENT_SIMILARITY_ALONE = 0.45              # 제목이 달라도 본문만으로 재게시 판단

# shadow mode 관측 기록 최소 기준 — 이보다 낮으면 완전 무관한 쌍이라 기록할 가치가 없다.
# enforce 임계값(0.35/0.45)보다 낮게 잡아서 "아깝게 안 걸린" 경계 사례까지 잡는다.
SHADOW_LOG_FLOOR = 0.15


def check_url_duplicate(conn: sqlite3.Connection, normalized_url: str) -> DuplicateCheck:
    existing = contents_repo.get_by_canonical_url(conn, normalized_url)
    if existing is None:
        return DuplicateCheck(is_duplicate=False)
    return DuplicateCheck(is_duplicate=True, reason="same_url", existing_content_id=existing["id"])


def check_content_duplicate(conn: sqlite3.Connection, content_hash: str) -> DuplicateCheck:
    existing = contents_repo.get_by_content_hash(conn, content_hash)
    if existing is None:
        return DuplicateCheck(is_duplicate=False)
    return DuplicateCheck(
        is_duplicate=True, reason="same_content_hash", existing_content_id=existing["id"],
    )


def load_fingerprint_cache(conn: sqlite3.Connection) -> list[dict]:
    """check_near_duplicate에 넘길 캐시를 한 번만 만든다 — {id, title_normalized, fingerprint(집합)}.

    run_collection이 실행 시작 시 한 번 호출해서 candidate마다 재사용한다(2026-08-31 성능 개선:
    예전엔 check_near_duplicate가 후보마다 DB를 다시 읽고 문자열을 다시 파싱했다 — 수백 개
    후보를 처리하는 동안 같은 지문 테이블을 수백 번 다시 읽는 꼴이었다).
    """
    return [
        {
            "id": row["id"], "title_normalized": row["title_normalized"] or "",
            "fingerprint": similarity.deserialize_fingerprint(row["content_fingerprint"]),
        }
        for row in contents_repo.list_fingerprints(conn)
    ]


def check_near_duplicate(
    fingerprint_cache: list[dict], normalized_title: str, fingerprint: frozenset[str]
) -> NearDuplicateCheck:
    """제목 유사도 + 본문 단어 집합 Jaccard로 재게시·경미 수정 재업로드를 잡는다 (10.3절 확장).

    check_url_duplicate/check_content_duplicate(완전일치)를 통과한 뒤에만 부른다. 정규화 제목이
    완전히 같으면 그 자체로 중복으로 본다(=title_similarity 1.0). fingerprint_cache는
    load_fingerprint_cache()로 한 번 만들어서 호출부가 들고 있다가 넘긴다 — 새로 저장한
    콘텐츠는 호출부가 직접 append한다(다시 쿼리하지 않음).
    """
    best_duplicate: NearDuplicateCheck | None = None  # enforce 판정용 (임계값 통과한 것 중 최고)
    best_overall_id: int | None = None                # shadow 로깅용 (임계값 무관 최고 content_sim)
    best_overall_title_sim = 0.0
    best_overall_content_sim = -1.0

    for row in fingerprint_cache:
        other_title = row["title_normalized"]
        other_fp = row["fingerprint"]

        if normalized_title and other_title == normalized_title:
            content_sim = similarity.content_similarity(fingerprint, other_fp)
            return NearDuplicateCheck(
                is_duplicate=True, matched_content_id=row["id"],
                title_similarity=1.0, content_similarity=content_sim,
                best_content_id=row["id"], best_title_similarity=1.0, best_content_similarity=content_sim,
            )

        title_sim = similarity.title_similarity(normalized_title, other_title)
        content_sim = similarity.content_similarity(fingerprint, other_fp)
        is_dup = (
            (title_sim >= TITLE_SIMILARITY_THRESHOLD and content_sim >= CONTENT_SIMILARITY_WITH_TITLE_MATCH)
            or content_sim >= CONTENT_SIMILARITY_ALONE
        )
        if is_dup and (best_duplicate is None or content_sim > best_duplicate.content_similarity):
            best_duplicate = NearDuplicateCheck(
                is_duplicate=True, matched_content_id=row["id"],
                title_similarity=title_sim, content_similarity=content_sim,
            )
        if content_sim > best_overall_content_sim:
            best_overall_id, best_overall_title_sim, best_overall_content_sim = row["id"], title_sim, content_sim

    result = best_duplicate or NearDuplicateCheck(is_duplicate=False)
    result.best_content_id = best_overall_id
    result.best_title_similarity = best_overall_title_sim
    result.best_content_similarity = max(best_overall_content_sim, 0.0)
    return result
