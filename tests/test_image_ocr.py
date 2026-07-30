from types import SimpleNamespace

from src.image_ocr import ImageOCR


def _ocr(monkeypatch, text):
    ocr = ImageOCR(SimpleNamespace(), {"enabled": True, "keep_only": True})
    monkeypatch.setattr("src.image_ocr.shutil.which", lambda _: "/bin/tesseract")
    monkeypatch.setattr(ocr, "_read", lambda _: text)
    return ocr


def test_ocr_runs_for_keep_short_image_post(monkeypatch):
    ocr = _ocr(monkeypatch, "이미지에서 추출한 의미 있는 문장입니다")
    discard = SimpleNamespace(body_text="", image_urls=["https://example.com/1.jpg"])
    assert ocr.enrich(discard, "discard") == "" and discard.body_text == ""   # keep 아님 → skip
    keep = SimpleNamespace(body_text="", image_urls=["https://example.com/1.jpg"])
    out = ocr.enrich(keep, "keep")
    assert out and keep.body_text == "[IMAGE_1_OCR]\n이미지에서 추출한 의미 있는 문장입니다"


def test_ocr_drops_meaningless_text(monkeypatch):
    ocr = _ocr(monkeypatch, "@#$%^&*|||...===")   # 특수문자 위주 → 무의미
    c = SimpleNamespace(body_text="", image_urls=["https://example.com/1.jpg"])
    assert ocr.enrich(c, "keep") == "" and c.body_text == ""


def test_ocr_worker_count_has_safe_minimum():
    assert ImageOCR(SimpleNamespace(), {"workers": 0}).workers == 1
