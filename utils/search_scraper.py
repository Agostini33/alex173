#!/usr/bin/env python3
"""
search_scraper_auto.py  —  собирает supplierId через поиск Wildberries
• сам ищет актуальный эндпоинт /vXX/search
• поддерживает alias-категории (preset=…&_st0=…)
• соблюдает лимит 20 req/min
• сохраняет BOTH:  product_brand  и  brand (supplierName)

Usage:
    python search_scraper_auto.py --query "head_accessories2" --pages 15 \
                                  --output raw_sellers.csv
"""

from __future__ import annotations

import argparse
import csv
import itertools
import random
import sys
import time
import urllib.parse as up

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── HTTP session ──────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
    "Accept": "application/json",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=5,
            backoff_factor=0.4,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
    ),
)

BASE_PARAMS = {
    "resultset": "catalog",
    "appType": 1,
    "curr": "rub",
    "dest": "-1257786",
    "lang": "ru",
    "locale": "ru",
    "spp": 30,
}


# ── autodetect /vXX/search ────────────────────────────────────────────────────
def pick_base_url() -> str:
    versions = range(25, 3, -1)  # v25 … v4
    bases = itertools.chain(
        (f"https://search.wb.ru/exactmatch/ru/common/v{v}/search" for v in versions),
        (f"https://search.wb.ru/exactmatch/sng/common/v{v}/search" for v in versions),
    )
    probe = {**BASE_PARAMS, "query": "тест", "page": 1}
    for url in bases:
        try:
            r = SESSION.get(url, params=probe, timeout=6)
            if r.ok and r.json().get("data", {}).get("products"):
                print("→ найден эндпоинт:", url)
                return url
        except Exception:
            pass
    raise RuntimeError("WB search API: ни один /vXX/ не дал данных")


BASE_URL = pick_base_url()


# ── helpers ───────────────────────────────────────────────────────────────────
def request(params: dict) -> dict:
    r = SESSION.get(BASE_URL, params=params, timeout=10)
    if r.status_code == 429:
        time.sleep(int(r.headers.get("Retry-After", 3)))
        return request(params)
    r.raise_for_status()
    return r.json()


def alias_to_preset(alias: str) -> dict[str, str] | None:
    meta = request({**BASE_PARAMS, "query": alias, "page": 1})
    if meta.get("data", {}).get("products"):
        return None  # alias уже выдаёт товары
    qstr = meta.get("query", "")
    return dict(up.parse_qsl(qstr)) if qstr.startswith("preset=") else None


def fetch_page(extra: dict, page: int) -> list[dict]:
    js = request({**BASE_PARAMS, **extra, "page": page})
    return js.get("data", {}).get("products", [])


# ── main crawl ────────────────────────────────────────────────────────────────
def crawl(query: str, pages: int) -> list[dict]:
    extra = alias_to_preset(query) or {"query": query}
    seen, rows = set(), []
    for p in range(1, pages + 1):
        prods = fetch_page(extra, p)
        if not prods:
            print(f"стр. {p}: пусто — стоп")
            break
        for pr in prods:
            sid = pr["supplierId"]
            if sid in seen:
                continue
            seen.add(sid)
            rows.append(
                {
                    "supplier_id": sid,
                    #  товарный бренд (обычно HEAD, NIKE …)
                    "product_brand": pr.get("brand", ""),
                    #  название продавца (supplier / supplierName)
                    "brand": pr.get("supplier") or pr.get("supplierName", ""),
                    "rating": pr.get("supplierRating", 0),
                    "articul": pr["id"],
                    "link": f"https://www.wildberries.ru/seller/{sid}",
                }
            )
        #  лимит 20 req/min → ≥3 с
        time.sleep(max(3.2, random.uniform(3.2, 4.5)))
    return rows


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True, help="слово или alias категории")
    ap.add_argument("--pages", type=int, default=10, help="сколько страниц")
    ap.add_argument("--output", default="raw_sellers.csv")
    args = ap.parse_args()

    try:
        rows = crawl(args.query, args.pages)
    except Exception as e:
        sys.exit(f"❌ {e}")

    if not rows:
        sys.exit("❌ ничего не найдено – измените запрос/alias")

    fieldnames = rows[0].keys()
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"✔️  сохранено {len(rows)} продавцов → {args.output}")


if __name__ == "__main__":
    main()
