"""실행 파이프라인 facade — 안정 공개 API.

실제 구현은 src/pipelines/ 에 있다(모드별 keyword/trend/gap_filling + 공용 헬퍼).
main.py·streamlit·tests가 참조하는 심볼을 여기서 재export한다. 새 코드는 되도록
`src.pipelines.<mode>`를 직접 import하되, 이 facade는 기존 호출 표면을 보존한다.
"""
from __future__ import annotations

from .pipelines.keyword import run
from .pipelines.trend import run_trend
from .pipelines.gap_filling import (
    _default_provider, _load_phase2_config, preview_discovery, preview_intents,
    preview_taxonomy_plan, run_targeted, run_taxonomy_plan, small_run, verify_unverified_candidates,
)
from .phase2.intent_builder import missing_manual_intents
# tests가 이름으로 참조하는 내부 헬퍼(안정 표면으로 재export)
from .pipelines.taxonomy_adjudication import _classification_status, _phase2_adjudicate
from .pipelines._trend_util import (
    _allocate_buckets, _append_unique_candidates,
    _extract_links, _round_robin_candidates,
)

__all__ = [
    "run", "run_trend", "run_targeted", "small_run", "verify_unverified_candidates",
    "preview_intents", "preview_discovery", "preview_taxonomy_plan", "run_taxonomy_plan", "missing_manual_intents",
]
