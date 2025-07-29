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
    right_desc = "Rasyan toothpaste description that is definitely long enough to pass"  # >50 chars
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

    def fake_get(url, timeout=10):
        if url.startswith("https://card.wb.ru/cards/detail"):
            return Resp(js)
        elif url.startswith("https://wbx-content-v2.wbstatic.net/ru"):
            return Resp({})
        raise AssertionError(url)

    monkeypatch.setattr(m.requests, "get", fake_get)

    text = m.wb_card_text(f"https://www.wildberries.ru/catalog/{nm}/detail.aspx")
    assert "Rasyan" in text
    assert "wrong" not in text
