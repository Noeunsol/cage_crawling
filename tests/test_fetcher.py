from src.fetcher import Fetcher


class _Response:
    status_code = 200
    content = b"image-bytes"
    headers = {"Content-Type": "application/octet-stream"}

    def raise_for_status(self):
        return None


def test_dcinside_image_uses_browser_headers_and_accepts_octet_stream(monkeypatch):
    fetcher = Fetcher({"http": {"respect_robots": False, "per_domain_delay": 0}})
    captured = {}

    class Session:
        def get(self, url, **kwargs):
            captured.update(kwargs.get("headers", {}))
            return _Response()

    fetcher._session = Session()
    data = fetcher.fetch_bytes("https://dcimg6.dcinside.co.kr/viewimage.php?id=x")
    assert data == b"image-bytes"
    assert captured["Referer"] == "https://gall.dcinside.com/"
    assert "Mozilla/5.0" in captured["User-Agent"]
