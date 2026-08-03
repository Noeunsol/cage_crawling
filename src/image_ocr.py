"""로컬 OCR. keep 콘텐츠 이미지를 검사하고 유의미한 텍스트만 본문에 추가한다."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
_HANGUL = re.compile(r"[가-힣]")
_SHORT_OCR_RISK = re.compile(
    r"죽여|살해|협박|자살|자해|성매매|성착취|몰카|신상|주소|전화번호|해킹|마약|폭행|사기|유포|공개하자"
)
_SHORT_OCR_PII = re.compile(
    r"01[016789][\s-]?\d{3,4}[\s-]?\d{4}|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"\d{2,3}[\s-]+\d{3,4}[\s-]+\d{4}"
)


def _meaningful(text: str, min_chars: int, exception_min_chars: int = 40) -> bool:
    """100자 이상은 유지하고, 40~99자는 위험·PII·문장성 근거가 있을 때만 유지한다."""
    stripped = _WS.sub("", text or "")
    if len(stripped) < exception_min_chars:
        return False
    useful = sum(1 for ch in stripped if ch.isalnum() or "가" <= ch <= "힣")
    if useful / len(stripped) < 0.5:
        return False
    if len(stripped) >= min_chars:
        return True
    hangul = len(_HANGUL.findall(text or ""))
    sentence_like = hangul >= 25 and bool(re.search(r"(?:다|요|까|라|함|했다|한다)[.!?…]?\s*$", text.strip()))
    return bool(_SHORT_OCR_RISK.search(text) or _SHORT_OCR_PII.search(text) or sentence_like)


def _clean_lines(text: str) -> str:
    """한 글자·기호·한영 혼합 OCR 잡음을 버리고 문장성 있는 줄만 남긴다."""
    kept = []
    for raw in (text or "").splitlines():
        line = _WS.sub(" ", raw).strip()
        compact = line.replace(" ", "")
        if len(compact) < 3:
            continue
        useful = sum(ch.isalnum() or "가" <= ch <= "힣" for ch in compact)
        if useful / len(compact) < 0.7:
            continue
        hangul = len(_HANGUL.findall(line))
        # 한국어 캡처에서 짧은 라틴 조각은 UI·아이콘 오인식일 가능성이 높다.
        if hangul < 2 and not (len(compact) >= 12 and len(line.split()) >= 2):
            continue
        kept.append(line)
    return "\n".join(dict.fromkeys(kept))


class ImageOCR:
    def __init__(self, fetcher, config: dict | None = None):
        cfg = config or {}
        self.fetcher = fetcher
        self.enabled = cfg.get("enabled", False)
        # 1차 keep 콘텐츠만 OCR (accepted_only는 구 config 호환 별칭)
        self.keep_only = cfg.get("keep_only", cfg.get("accepted_only", True))
        self.scan_all_keep_images = cfg.get("scan_all_keep_images", False)
        self.body_threshold = int(cfg.get("body_threshold", 200))
        self.multi_image_threshold = int(cfg.get("multi_image_threshold", 3))
        self.multi_image_body_threshold = int(cfg.get("multi_image_body_threshold", 800))
        self.max_chars_per_image = int(cfg.get("max_chars_per_image", 100))
        self.max_images = int(cfg.get("max_images", 10))
        self.max_chars = int(cfg.get("max_ocr_chars", 6000))
        self.max_tile_width = int(cfg.get("max_tile_width", cfg.get("max_dimension", 4000)))
        self.max_tiles = int(cfg.get("max_tiles", 8))               # 세로 타일 수 상한
        self.min_meaningful = int(cfg.get("min_meaningful_chars", 15))
        self.exception_min_chars = int(cfg.get("exception_min_chars", 40))
        self.language = cfg.get("language", "kor+eng")
        self.tile_height = int(cfg.get("tile_height", 2000))
        self.overlap = int(cfg.get("overlap", 100))
        self.workers = max(1, int(cfg.get("workers", 2)))

    def enrich(self, content, filter_action: str) -> str:
        """keep 콘텐츠 이미지를 OCR하고 유의미한 결과만 본문에 추가해 반환한다."""
        images = list(content.image_urls or [])
        body_len = len(content.body_text or "")
        image_dependent = (
            body_len < self.body_threshold
            or (
                body_len < self.multi_image_body_threshold
                and (
                    len(images) >= self.multi_image_threshold
                    or body_len / max(len(images), 1) < self.max_chars_per_image
                )
            )
        )
        if (
            not self.enabled
            or self.keep_only and filter_action not in ("keep", "accepted")
            or not images
            or not (self.scan_all_keep_images or image_dependent)
            or not shutil.which("tesseract")
        ):
            return ""
        chunks = []
        for number, url in enumerate(images[:self.max_images], 1):
            text = self._read(url)
            if _meaningful(text, self.min_meaningful, self.exception_min_chars):
                chunks.append(f"[IMAGE_{number}_OCR]\n{text}")
            if sum(map(len, chunks)) >= self.max_chars:  # 조기 종료
                break
        if not chunks:
            return ""
        ocr_text = "\n\n".join(chunks)[:self.max_chars]
        content.ocr_image_count = len(chunks)
        content.ocr_char_count = len(_WS.sub("", ocr_text))
        # max_chars는 OCR 결과 상한이다. 기존 본문을 잘라내지 않는다.
        content.body_text = "\n\n".join(filter(None, [content.body_text.strip(), ocr_text]))
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
        step = max(1, self.tile_height - self.overlap)
        with tempfile.TemporaryDirectory(prefix="taxonomy-ocr-") as tmp:
            paths = []
            for index, top in enumerate(range(0, image.height, step)):
                if index >= self.max_tiles:              # 타일 수 상한
                    break
                tile = image.crop((0, top, image.width, min(top + self.tile_height, image.height)))
                if tile.width > self.max_tile_width:     # 긴 캡처는 세로를 유지하고 타일 폭만 축소
                    ratio = self.max_tile_width / tile.width
                    tile = tile.resize((self.max_tile_width, max(1, int(tile.height * ratio))))
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
        variants = []
        try:
            from PIL import Image, ImageEnhance, ImageFilter, ImageOps
            image = Image.open(path).convert("RGB")
            gray = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=1)
            if gray.width < 1800:
                scale = min(2.0, 1800 / max(gray.width, 1))
                gray = gray.resize(
                    (int(gray.width * scale), int(gray.height * scale)), Image.Resampling.LANCZOS,
                )
            gray = ImageEnhance.Contrast(gray).enhance(1.35).filter(ImageFilter.SHARPEN)
            prepared = f"{path}.prepared.png"
            gray.save(prepared)
            # 기사/대화 캡처는 단일 본문(6), 흩어진 텍스트는 sparse text(11)가 유리하다.
            variants = [self._ocr_tsv(prepared, 6), self._ocr_tsv(prepared, 11)]
        except (ImportError, OSError):
            return ""
        candidates = [(score, _clean_lines(text)) for score, text in variants]
        candidates = [(score, text) for score, text in candidates if text]
        if not candidates:
            return ""
        # 신뢰도와 보존된 문장 길이를 함께 보되, 긴 잡음이 점수를 독점하지 않게 완만하게 가산한다.
        return max(candidates, key=lambda item: item[0] + min(len(item[1]), 500) / 100)[1]

    def _ocr_tsv(self, path: str, psm: int) -> tuple[float, str]:
        """Tesseract 단어 confidence를 이용해 낮은 신뢰도 토큰을 제거한다."""
        try:
            result = subprocess.run(
                ["tesseract", path, "stdout", "-l", self.language,
                 "--psm", str(psm), "tsv"],
                capture_output=True, text=True, timeout=60, check=False,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return 0.0, ""
        if result.returncode != 0:
            return 0.0, ""
        lines: defaultdict = defaultdict(list)
        confidences = []
        for row in result.stdout.splitlines()[1:]:
            cols = row.split("\t", 11)
            if len(cols) != 12 or not cols[11].strip():
                continue
            try:
                confidence = float(cols[10])
            except ValueError:
                continue
            if confidence < 35:
                continue
            key = tuple(cols[i] for i in (1, 2, 3, 4))
            lines[key].append(cols[11].strip())
            confidences.append(confidence)
        text = "\n".join(" ".join(words) for words in lines.values())
        return (sum(confidences) / len(confidences) if confidences else 0.0), text
