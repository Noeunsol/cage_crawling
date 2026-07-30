"""선별적 로컬 OCR. 외부 API 없이 tesseract가 설치된 환경에서만 동작한다."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")


def _meaningful(text: str, min_chars: int) -> bool:
    """OCR 결과가 의미 있는지: 최소 길이 + 한글/영숫자 비율(노이즈·특수문자 덩어리 제외)."""
    stripped = _WS.sub("", text or "")
    if len(stripped) < min_chars:
        return False
    useful = sum(1 for ch in stripped if ch.isalnum() or "가" <= ch <= "힣")
    return useful / len(stripped) >= 0.5


class ImageOCR:
    def __init__(self, fetcher, config: dict | None = None):
        cfg = config or {}
        self.fetcher = fetcher
        self.enabled = cfg.get("enabled", False)
        # 1차 keep 콘텐츠만 OCR (accepted_only는 구 config 호환 별칭)
        self.keep_only = cfg.get("keep_only", cfg.get("accepted_only", True))
        self.body_threshold = int(cfg.get("body_threshold", 200))
        self.max_images = int(cfg.get("max_images", 5))
        self.max_chars = int(cfg.get("max_ocr_chars", 6000))
        self.max_dimension = int(cfg.get("max_dimension", 4000))   # 한 변 px 상한(다운스케일)
        self.max_tiles = int(cfg.get("max_tiles", 8))               # 세로 타일 수 상한
        self.min_meaningful = int(cfg.get("min_meaningful_chars", 15))
        self.language = cfg.get("language", "kor+eng")
        self.tile_height = int(cfg.get("tile_height", 2000))
        self.overlap = int(cfg.get("overlap", 100))
        self.workers = max(1, int(cfg.get("workers", 2)))

    def enrich(self, content, filter_action: str) -> str:
        """이미지 의존(짧은 본문+이미지) keep 콘텐츠를 로컬 OCR로 보강. 추가한 OCR 텍스트를 반환."""
        if (
            not self.enabled
            or self.keep_only and filter_action not in ("keep", "accepted")
            or not content.image_urls
            or len(content.body_text or "") >= self.body_threshold
            or not shutil.which("tesseract")
        ):
            return ""
        chunks = []
        for number, url in enumerate(content.image_urls[:self.max_images], 1):
            text = self._read(url)
            if _meaningful(text, self.min_meaningful):   # 무의미 OCR 제외
                chunks.append(f"[IMAGE_{number}_OCR]\n{text}")
            if sum(map(len, chunks)) >= self.max_chars:  # 조기 종료
                break
        if not chunks:
            return ""
        ocr_text = "\n\n".join(chunks)[:self.max_chars]
        content.body_text = "\n\n".join(
            filter(None, [content.body_text.strip(), ocr_text])
        )[:self.max_chars]
        return ocr_text

    def _read(self, url: str) -> str:
        raw = self.fetcher.fetch_bytes(url)
        if not raw:
            return ""
        try:
            from PIL import Image
            image = Image.open(BytesIO(raw)).convert("RGB")
        except (ImportError, OSError):
            return ""
        longest = max(image.width, image.height)
        if longest > self.max_dimension:                 # 크기 상한 → 다운스케일
            ratio = self.max_dimension / longest
            image = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))))
        step = max(1, self.tile_height - self.overlap)
        with tempfile.TemporaryDirectory(prefix="taxonomy-ocr-") as tmp:
            paths = []
            for index, top in enumerate(range(0, image.height, step)):
                if index >= self.max_tiles:              # 타일 수 상한
                    break
                tile = image.crop((0, top, image.width, min(top + self.tile_height, image.height)))
                path = f"{tmp}/{index}.png"
                tile.save(path)
                paths.append(path)
            parts = []
            with ThreadPoolExecutor(max_workers=min(self.workers, len(paths) or 1)) as pool:
                for text in pool.map(self._ocr_tile, paths):
                    if text:
                        parts.append(text)
                    if sum(map(len, parts)) >= self.max_chars:   # 조기 종료
                        break
        return "\n".join(parts)[:self.max_chars]

    def _ocr_tile(self, path: str) -> str:
        try:
            result = subprocess.run(
                ["tesseract", path, "stdout", "-l", self.language],
                capture_output=True, text=True, timeout=60, check=False,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""
