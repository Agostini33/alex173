import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def reload_main():
    if "main" in sys.modules:
        del sys.modules["main"]
    return importlib.import_module("main")


def test_select_correct_product(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    m = reload_main()
    nm = 12345
    right_desc = (
        "Rasyan toothpaste description that is definitely long enough to pass"
    )
    wrong_desc = "wrong product"

    js = {
        "data": {
            "products": [
                {"id": nm + 1, "description": wrong_desc},
                {"id": nm, "description": right_desc},
            ]
        }
    }

    class Resp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

        @property
        def headers(self):
            return {"Content-Type": "application/json"}

    def fake_get(url, timeout=10, allow_redirects=True):
        if url.startswith("https://card.wb.ru/cards/detail"):
            return Resp(js)
        if "basket" in url:
            return Resp({})
        raise AssertionError(url)

    class DummySession:
        def __init__(self):
            self.headers = {}

        def get(self, url, timeout=10, allow_redirects=True):
            return fake_get(url, timeout, allow_redirects)

    monkeypatch.setattr(m.requests, "Session", lambda: DummySession())

    text = m.wb_card_text(f"https://www.wildberries.ru/catalog/{nm}/detail.aspx")
    assert "Rasyan" in text
    assert "wrong" not in text


def test_basket_first(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    m = reload_main()
    nm = 54321
    js = {
        "descriptionHtml": "<p>Good desc from basket that is definitely long enough for testing purposes and beyond</p>"
    }

    class Resp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

        @property
        def headers(self):
            return {"Content-Type": "application/json"}

    def fake_get(url, timeout=10, allow_redirects=True):
        if url.startswith("https://basket-"):
            return Resp(js)
        if "static-basket" in url:
            return Resp({})
        if url.startswith("https://card.wb.ru"):
            return Resp({"data": {"products": [{"id": nm, "description": "wrong"}]}})
        raise AssertionError(url)

    class DummySession:
        def __init__(self):
            self.headers = {}

        def get(self, url, timeout=10, allow_redirects=True):
            return fake_get(url, timeout, allow_redirects)

    monkeypatch.setattr(m.requests, "Session", lambda: DummySession())
    text = m.wb_card_text(f"https://www.wildberries.ru/catalog/{nm}/detail.aspx")
    assert "Good desc from basket" in text
    assert "wrong" not in text

