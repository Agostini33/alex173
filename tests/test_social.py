import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "utils"))
import social_scraper as ss


class FakeResp:
    def __init__(self, text="", js=None):
        self.text = text
        self._js = js or {}

    def json(self):
        return self._js


def test_social_scraper(monkeypatch, tmp_path):
    def fake_get(url, timeout=10):
        if "wildberries.ru" in url:
            return FakeResp(text="ИНН: 1234567890")
        if "api-fns.ru" in url:
            js = {
                "items": [
                    {
                        "СвКонтактДл": [{"Телефон": "+7 (926) 123-45-67"}],
                        "СвАдресЮЛ": {"ЭлПочта": "test@mail.ru"},
                    }
                ]
            }
            return FakeResp(js=js)
        raise AssertionError("unexpected url")

    monkeypatch.setattr(ss.SESSION, "get", lambda url, timeout=10: fake_get(url, timeout))

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
