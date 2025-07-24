#!/usr/bin/env python3
import argparse
import asyncio
import csv
import json
import random
import re

import requests
from playwright.async_api import async_playwright
from tqdm import tqdm

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux) Chrome/126 Safari/537.36"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.timeout = 10


CARD_URL = "https://card.wb.ru/cards/v1/detail"

PHONE_RE = re.compile(r"(?:\+7|8)\s*\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")

CONTACT_RE = {
    "phone": PHONE_RE,
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "telegram": re.compile(r"(?:t\.me/|telegram\.me/)[A-Za-z0-9_]+"),
    "whatsapp": re.compile(r"(?:wa\.me/)\d+"),
    "site": re.compile(r"https?://[^\s\"']+"),
}


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)[-10:]
    return f"+7{digits}" if len(digits) == 10 else ""


def fetch_contacts_from_card(nm: int):
    p = {"appType": 1, "curr": "rub", "dest": "-1257786", "nm": nm}
    r = SESSION.get(CARD_URL, params=p, timeout=10)
    if r.ok:
        js = r.json()
        prod = js.get("data", {}).get("products", [{}])[0]
        desc = prod.get("description", "")
        seller = json.dumps(prod.get("sellerInfo", ""), ensure_ascii=False)
        return parse_text(f"{desc} {seller}")
    return {}


async def render_and_parse(url: str, page):
    await page.goto(url, timeout=20000)
    await page.wait_for_load_state("networkidle")
    text = await page.locator("body").inner_text()
    return parse_text(text)


def parse_text(text: str):
    found = {}
    for k, rx in CONTACT_RE.items():
        m = rx.search(text)
        if m:
            val = m.group(0)
            if k == "phone":
                val = normalize_phone(val)
            found[k] = val
    return found


async def async_main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="raw_sellers.csv")
    ap.add_argument("--output", default="socials.csv")
    ap.add_argument("--delay", type=float, default=0.15)
    ap.add_argument("--render-limit", type=int, default=80)
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

    browser = None
    playwright = None
    page = None
    renders = 0

    async def get_page():
        nonlocal browser, playwright, page
        if page is None:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch()
            context = await browser.new_context()
            page = await context.new_page()
        return page

    for r in tqdm(rows):
        try:
            contacts = fetch_contacts_from_card(int(r["articul"]))
            if not contacts and renders < a.render_limit:
                pg = await get_page()
                contacts = await render_and_parse(r["link"], pg)
                renders += 1
            for k in ("telegram", "whatsapp", "email", "phone", "site"):
                r[k] = contacts.get(k, "")
        except Exception:
            for k in ("telegram", "whatsapp", "email", "phone", "site"):
                r[k] = ""
        await asyncio.sleep(a.delay + random.uniform(0, a.delay))

    if browser:
        await browser.close()
        await playwright.stop()
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fn)
        w.writeheader()
        w.writerows(rows)
    print("Done:", a.output)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
