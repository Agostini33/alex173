#!/usr/bin/env python3
import argparse
import csv
import re
import time

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

HEAD = {"User-Agent": "Mozilla/5.0 (X11; Linux) Chrome/126 Safari/537.36"}
TG_RE = re.compile(r"(https?://t\.me/[A-Za-z0-9_]+|@[A-Za-z0-9_]{4,})", re.I)
WA_RE = re.compile(r"https?://(?:wa\.me|api\.whatsapp\.com)/\d+", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(\+?7\d{10}|8\d{10})")
S = requests.Session()
S.headers.update(HEAD)
S.timeout = 10


def parse(html: str):
    soup = BeautifulSoup(html, "html.parser")
    t = soup.get_text(" ", strip=True)
    tg = TG_RE.search(t)
    wa = WA_RE.search(t)
    em = EMAIL_RE.search(t)
    ph = PHONE_RE.search(t)
    for a in soup.find_all("a", href=True):
        if not tg and (m := TG_RE.search(a["href"])):
            tg = m
        if not wa and (m := WA_RE.search(a["href"])):
            wa = m
        if not em and (m := EMAIL_RE.search(a["href"])):
            em = m
        if not ph and (m := PHONE_RE.search(a["href"])):
            ph = m
    return (
        tg.group(0) if tg else "",
        wa.group(0) if wa else "",
        em.group(0) if em else "",
        ph.group(0) if ph else "",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="raw_sellers.csv")
    ap.add_argument("--output", default="socials.csv")
    ap.add_argument("--delay", type=float, default=0.15)
    a = ap.parse_args()
    with open(a.input, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fn = (reader.fieldnames or []) + ["telegram", "whatsapp", "email", "phone"]
    if not rows:
        # nothing to scrape, create empty output with header
        csv.DictWriter(
            open(a.output, "w", newline="", encoding="utf-8"), fieldnames=fn
        ).writeheader()
        print("Done:", a.output)
        return
    for r in tqdm(rows):
        try:
            h = S.get(f"https://www.wildberries.ru/seller/{r['supplier_id']}").text
            (
                r["telegram"],
                r["whatsapp"],
                r["email"],
                r["phone"],
            ) = parse(h)
        except:
            r["telegram"] = r["whatsapp"] = r["email"] = r["phone"] = ""
        time.sleep(a.delay)
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fn)
        w.writeheader()
        w.writerows(rows)
    print("Done:", a.output)


if __name__ == "__main__":
    main()
