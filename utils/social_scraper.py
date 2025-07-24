#!/usr/bin/env python3
import argparse
import csv
import json
import re
import random
import time

import requests
import requests_html
from tqdm import tqdm

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux) Chrome/126 Safari/537.36"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.timeout = 10


CARD_URL = "https://card.wb.ru/cards/v1/detail"
CONTACT_RE = {
    "phone": re.compile(r"(?:\+?7|8)\d{9,10}"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "telegram": re.compile(r"(?:t\.me/|telegram\.me/)[A-Za-z0-9_]+"),
    "whatsapp": re.compile(r"(?:wa\.me/)\d+"),
    "site": re.compile(r"https?://[^\s\"']+"),
}


def fetch_contacts_from_card(nm: int):
    p = {"appType": 1, "curr": "rub", "dest": "-1257786", "nm": nm}
    r = SESSION.get(CARD_URL, params=p, timeout=10)
    if r.ok:
        js = r.json()
        desc = js.get("data", {}).get("products", [{}])[0].get("description", "")
        return parse_text(desc)
    return {}


def render_and_parse(url: str):
    ses = requests_html.HTMLSession()
    r = ses.get(url, headers=HEADERS, timeout=20)
    r.html.render(timeout=20, sleep=1)
    return parse_text(r.html.text)


def parse_text(text: str):
    found = {}
    for k, rx in CONTACT_RE.items():
        m = rx.search(text)
        if m:
            found[k] = m.group(0)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="raw_sellers.csv")
    ap.add_argument("--output", default="socials.csv")
    ap.add_argument("--delay", type=float, default=0.15)
    a = ap.parse_args()
    with open(a.input, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fn = (reader.fieldnames or []) + [
            "telegram",
            "whatsapp",
            "email",
            "phone",
            "site",
        ]
    if not rows:
        # nothing to scrape, create empty output with header
        csv.DictWriter(
            open(a.output, "w", newline="", encoding="utf-8"), fieldnames=fn
        ).writeheader()
        print("Done:", a.output)
        return
    for r in tqdm(rows):
        try:
            contacts = fetch_contacts_from_card(int(r["articul"]))
            if not contacts:
                contacts = render_and_parse(r["link"])
            for k in ("telegram", "whatsapp", "email", "phone", "site"):
                r[k] = contacts.get(k, "")
        except Exception:
            for k in ("telegram", "whatsapp", "email", "phone", "site"):
                r[k] = ""
        time.sleep(a.delay + random.uniform(0, a.delay))
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fn)
        w.writeheader()
        w.writerows(rows)
    print("Done:", a.output)


if __name__ == "__main__":
    main()
