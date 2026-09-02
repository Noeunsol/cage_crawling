"""댓글 후보 중 대표 댓글을 고르는 순수 선택 로직.

사이트별 파서가 댓글을 실제로 긁어오는 부분은 아직 없다 — 이 모듈은 "댓글 후보 목록이 이미 있을 때
어떤 걸 대표로 남길지"만 담당한다. 유해 신호(post_harm_signal/comment_harm_signal) 판정 로직도 아직
없다 — 원글과 댓글을 별도 필드로 들고 다닐 수 있는 그릇(ExtractionResult)만 여기 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Comment:
    text: str
    taxonomy_relevance: float = 0.0
    reaction_count: int = 0
    is_representative_or_author_selected: bool = False


@dataclass
class ExtractionResult:
    post_content: str
    selected_comments: list[Comment] = field(default_factory=list)
    post_harm_signal: bool | None = None       # 원글 자체의 유해 여부. 판정 로직은 아직 없음(외부 주입)
    comment_harm_signal: bool | None = None    # 댓글 중 유해 응답 존재 여부. 판정 로직은 아직 없음(외부 주입)


_PRIORITY_KEYS = {
    "taxonomy_relevance": lambda c: c.taxonomy_relevance,
    "reaction_count": lambda c: c.reaction_count,
    "representative_or_author_selected": lambda c: c.is_representative_or_author_selected,
}


def select_comments(comments: list[Comment], *, max_comments: int, selection_priority: list[str]) -> list[Comment]:
    """selection_priority(configs/extraction.yaml의 comment_extraction.selection_priority) 순서대로
    내림차순 정렬해서 상위 max_comments개만 남긴다.
    """
    keys = [_PRIORITY_KEYS[name] for name in selection_priority if name in _PRIORITY_KEYS]
    ranked = sorted(comments, key=lambda c: tuple(k(c) for k in keys), reverse=True)
    return ranked[:max_comments]
