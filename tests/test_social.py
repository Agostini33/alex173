import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "utils"))
import social_scraper as ss


class FakeResp:
    def __init__(self, js=None, text=""):
        self._js = js
        self.text = text
        self.ok = True

    def json(self):
        return self._js


def test_social_links(monkeypatch, tmp_path):
    js = {
        "data": {"products": [{"description": "call +79991234567 or test@mail.ru"}]}
    }
    monkeypatch.setattr(
        ss.SESSION, "get", lambda url, params, timeout: FakeResp(js)
    )
    monkeypatch.setattr(ss, "render_and_parse", lambda url: {})
    monkeypatch.setattr(ss.time, "sleep", lambda x: None)
    input_csv = tmp_path / "raw.csv"
    with open(input_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["supplier_id", "articul", "link"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "supplier_id": "1",
                "articul": "10",
                "link": "http://example.com",
            }
        )
    output_csv = tmp_path / "out.csv"
    argv = ["social_scraper.py", "--input", str(input_csv), "--output", str(output_csv)]
    monkeypatch.setattr(sys, "argv", argv)
    ss.main()
    rows = list(csv.DictReader(open(output_csv, encoding="utf-8")))
    assert rows[0]["email"] == "test@mail.ru"
    assert rows[0]["phone"] == "+79991234567"
