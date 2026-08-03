from types import SimpleNamespace

from src.image_ocr import ImageOCR, _clean_lines, _meaningful


def _ocr(monkeypatch, text):
    ocr = ImageOCR(SimpleNamespace(), {
        "enabled": True, "keep_only": True,
        "min_meaningful_chars": 15, "exception_min_chars": 10,
    })
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
    assert keep.ocr_image_count == 1 and keep.ocr_char_count > 0


def test_ocr_drops_meaningless_text(monkeypatch):
    ocr = _ocr(monkeypatch, "@#$%^&*|||...===")   # 특수문자 위주 → 무의미
    c = SimpleNamespace(body_text="", image_urls=["https://example.com/1.jpg"])
    assert ocr.enrich(c, "keep") == "" and c.body_text == ""


def test_ocr_runs_for_multi_image_post_with_short_caption(monkeypatch):
    ocr = _ocr(monkeypatch, "이미지에서 추출한 의미 있는 문장입니다")
    c = SimpleNamespace(body_text="설명" * 121, image_urls=[f"https://example.com/{i}.jpg" for i in range(15)])
    assert len(c.body_text) == 242
    assert ocr.enrich(c, "keep")


def test_ocr_scans_long_body_when_all_keep_images_enabled(monkeypatch):
    ocr = ImageOCR(SimpleNamespace(), {
        "enabled": True, "keep_only": True, "scan_all_keep_images": True,
        "min_meaningful_chars": 15, "exception_min_chars": 10,
    })
    monkeypatch.setattr("src.image_ocr.shutil.which", lambda _: "/bin/tesseract")
    monkeypatch.setattr(ocr, "_read", lambda _: "뉴스 캡처 이미지에 포함된 유의미한 기사 본문입니다")
    original = "긴 본문" * 3000
    c = SimpleNamespace(body_text=original, image_urls=["https://example.com/news.jpg"])
    out = ocr.enrich(c, "keep")
    assert out
    assert c.body_text.startswith(original)               # 기존 긴 본문을 자르지 않음
    assert "[IMAGE_1_OCR]" in c.body_text


def test_ocr_worker_count_has_safe_minimum():
    assert ImageOCR(SimpleNamespace(), {"workers": 0}).workers == 1


def test_ocr_noise_lines_are_removed():
    noisy = """4
뜨 5 a
t
UI 이 새끼가 이거 히
너내믿 e 내 밑에서 are Ben
아닙니다 제가 미처 생각 못했습니다
게 보지 말고 씨 ana ae 트 그 페"""
    cleaned = _clean_lines(noisy)
    assert "\n4\n" not in f"\n{cleaned}\n"
    assert "뜨 5 a" not in cleaned
    assert "아닙니다 제가 미처 생각 못했습니다" in cleaned


def test_short_ocr_requires_risk_pii_or_sentence_context():
    assert not _meaningful("행사 안내 이미지입니다 자세한 내용은 홈페이지에서 확인", 100, 40)
    assert _meaningful("너 죽여버린다 집 주소 알고 있으니까 조심해라 정말 가만두지 않겠다 주변 사람들에게도 전부 알리고 찾아가겠다", 100, 40)
    assert _meaningful("연락처는 010-1234-5678이며 이 번호를 모두에게 공개하자 다른 사람과 여러 커뮤니티에도 전달해라", 100, 40)
    assert not _meaningful("자살", 100, 40)  # 40자 미만은 위험 단어가 있어도 잡음 가능성이 커서 제외
