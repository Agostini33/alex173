import os, sys, csv
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'utils'))
import social_scraper as ss

class FakeResp:
    def __init__(self, text):
        self.text = text

def test_social_links(monkeypatch, tmp_path):
    html = '<a href="https://t.me/test">tg</a> <a href="https://wa.me/123">wa</a>'
    monkeypatch.setattr(ss.S, 'get', lambda url: FakeResp(html))
    monkeypatch.setattr(ss.time, 'sleep', lambda x: None)
    input_csv = tmp_path / 'raw.csv'
    with open(input_csv, 'w', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow(['supplier_id'])
        csv.writer(f).writerow(['1'])
    output_csv = tmp_path / 'out.csv'
    argv = ['social_scraper.py', '--input', str(input_csv), '--output', str(output_csv)]
    monkeypatch.setattr(sys, 'argv', argv)
    ss.main()
    rows = list(csv.DictReader(open(output_csv, encoding='utf-8')))
    assert rows[0]['telegram'] == 'https://t.me/test'
    assert rows[0]['whatsapp'] == 'https://wa.me/123'
