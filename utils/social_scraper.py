#!/usr/bin/env python3
import csv, re, time, argparse, requests
from bs4 import BeautifulSoup
from tqdm import tqdm

HEAD = {"User-Agent": "Mozilla/5.0 (X11; Linux) Chrome/126 Safari/537.36"}
TG_RE = re.compile(
    r"(https?://t\.me/[A-Za-z0-9_]+|tg://resolve\?domain=[A-Za-z0-9_]+|@[A-Za-z0-9_]{4,})",
    re.I,
)
WA_RE = re.compile(
    r"https?://(?:wa\.me|api\.whatsapp\.com)/\d+|whatsapp://send\?phone=\d+",
    re.I,
)

def parse(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    tg = TG_RE.search(text)
    wa = WA_RE.search(text)
    for a in soup.find_all("a", href=True):
        if not tg and TG_RE.search(a["href"]):
            tg = TG_RE.search(a["href"])
        if not wa and WA_RE.search(a["href"]):
            wa = WA_RE.search(a["href"])
    return (tg.group(0) if tg else "", wa.group(0) if wa else "")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="raw_sellers.csv")
    ap.add_argument("--output", default="contacts.csv")
    ap.add_argument("--delay", type=float, default=0.15)
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.input, newline="", encoding="utf-8")))
    fn = list(rows[0].keys()) + ["telegram", "whatsapp"]

    for r in tqdm(rows):
        try:
            resp = requests.get(
                f"https://www.wildberries.ru/seller/{r['supplier_id']}",
                headers=HEAD,
                timeout=10,
            )
            r["telegram"], r["whatsapp"] = parse(resp.text)
        except Exception:
            r["telegram"] = r["whatsapp"] = ""
        time.sleep(a.delay)

    with open(a.output, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fn).writeheader()
        csv.writer(f).writerows([r.values() for r in rows])
    print("Done:", a.output)

if __name__=='__main__': main()

