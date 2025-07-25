import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "utils"))
import types

class DummyScraper:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def get(self, url, headers=None, timeout=10):
        return ""

async def create_scraper():
    return DummyScraper()

sys.modules['aiocfscrape'] = types.SimpleNamespace(create_scraper=create_scraper)
import social_scraper as ss


class FakeResp:
    def __init__(self, text="", js=None):
        self.text = text
        self._js = js or {}

    def json(self):
        return self._js


def test_social_scraper(monkeypatch, tmp_path):
    async def fake_get_inn_cf(sid: str, scraper):
        return "1234567890"

    async def fake_query_fns(session, inn: str):
        return "+7 (926) 123-45-67", "test@mail.ru"

    monkeypatch.setattr(ss, "get_inn_cf", fake_get_inn_cf)
    monkeypatch.setattr(ss, "query_fns", fake_query_fns)

    inp = tmp_path / "raw.csv"
    with open(inp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["supplier_id"])
        w.writeheader()
        w.writerow({"supplier_id": "1"})

    outp = tmp_path / "out.csv"
    monkeypatch.setattr(sys, "argv", ["social_scraper.py", "--input", str(inp), "--output", str(outp)])
    ss.main()
    rows = list(csv.DictReader(open(outp, encoding="utf-8")))
    assert rows[0]["inn"] == "1234567890"
    assert rows[0]["phone"] == "+79261234567"
    assert rows[0]["email"] == "test@mail.ru"
