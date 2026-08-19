"""단계별 파일 저장 — content_id별 디렉토리에 raw/cleaned/masked를 남긴다(재현·디버깅용).

DB(content_records)와 별개의 opt-in 보조 저장소. staging.enabled=false면 완전 no-op.
파일↔DB 상관은 content_id로 도출(DB 컬럼 추가 없음).
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import paths
from .schema import content_id_for

log = logging.getLogger(__name__)

_TEXT_STAGES = (("raw_text", "raw_text", "raw.txt"),
                ("cleaned", "cleaned_text", "cleaned.txt"),
                ("masked", "masked_text", "masked.txt"))
_UNMASKED = {"raw_text", "cleaned"}


class ArtifactStore:
    def __init__(self, base_dir: str = paths.STAGES_DIR,
                 stages: frozenset[str] = frozenset(), save_raw_text: bool = True):
        self.base = Path(base_dir)
        self.stages = set(stages)
        self.save_raw_text = save_raw_text

    @classmethod
    def from_settings(cls, settings: dict) -> "ArtifactStore":
        st = settings.get("staging", {}) or {}
        if not st.get("enabled"):
            return cls(stages=frozenset())          # no-op
        enabled = {k for k, v in (st.get("stages") or {}).items() if v}
        return cls(base_dir=st.get("base_dir", paths.STAGES_DIR), stages=frozenset(enabled),
                   save_raw_text=True)

    def _enabled(self, stage: str) -> bool:
        if stage not in self.stages:
            return False
        return self.save_raw_text or stage not in _UNMASKED

    def _write(self, content_id: str, fname: str, text: str) -> None:
        try:
            d = self.base / content_id
            d.mkdir(parents=True, exist_ok=True)
            (d / fname).write_text(text, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 (저장 실패가 파이프라인을 깨지 않는다)
            log.debug("staged 저장 실패 %s/%s: %s", content_id, fname, exc)

    def save_record(self, rec) -> None:
        if not self.stages:
            return
        cid = getattr(rec, "content_id", "") or content_id_for(getattr(rec, "source_url", ""))
        for stage, attr, fname in _TEXT_STAGES:
            if self._enabled(stage):
                val = getattr(rec, attr, None)
                if val:
                    self._write(cid, fname, val)


if __name__ == "__main__":
    import tempfile
    from types import SimpleNamespace

    base = tempfile.mkdtemp()
    rec = SimpleNamespace(content_id="abc123", source_url="http://x",
                          raw_text="RAW", cleaned_text="CLEAN", masked_text="MASK")

    # off → no-op
    ArtifactStore.from_settings({"staging": {"enabled": False}}).save_record(rec)
    assert not list(Path(base).glob("*")), "disabled인데 파일 생성됨"

    # masked만 (기본 안전)
    s = ArtifactStore(base_dir=base, stages=frozenset({"masked", "cleaned", "raw_text"}), save_raw_text=False)
    s.save_record(rec)
    d = Path(base) / "abc123"
    assert (d / "masked.txt").read_text() == "MASK"
    assert not (d / "cleaned.txt").exists() and not (d / "raw.txt").exists(), "save_raw_text=False인데 미마스킹 저장됨"

    # save_raw_text=True면 cleaned/raw도
    s2 = ArtifactStore(base_dir=base, stages=frozenset({"cleaned"}), save_raw_text=True)
    s2.save_record(rec)
    assert (d / "cleaned.txt").read_text() == "CLEAN"
    print("artifact_store self-check OK")
