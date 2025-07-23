#!/usr/bin/env python3
"""search_scraper.py
Собирает supplier_id по ключевому слову через Wildberries search API.
Версия: 2025-07-22

Пример запуска:
    python search_scraper.py --query "шапка" --pages 10 --output raw_sellers.csv
"""

import csv, argparse, requests, time
from tqdm import tqdm

HEAD = {"User-Agent": "Mozilla/5.0 (X11; Linux) Chrome/126 Safari/537.36"}
API  = "https://search.wb.ru/exactmatch/ru/common/v13/search"


def page(q, p):
    params = {"query": q, "page": p, "spp": 30, "resultset": "catalog"}
    r = requests.get(API, params=params, headers=HEAD, timeout=10)
    r.raise_for_status()
    return r.json()


def collect(query, pages):
    ids = set()
    for p in tqdm(range(1, pages + 1), desc="стр."):
        try:
            data = page(query, p)
            for d in data.get("data", {}).get("products", []):
                ids.add(d["supplier_id"])
        except Exception as e:
            print(f"⚠️  p{p}: {e}")
            break
        time.sleep(0.15)
    return sorted(ids)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--pages", type=int, default=5)
    ap.add_argument("--output", default="raw_sellers.csv")
    a = ap.parse_args()

    ids = collect(a.query, a.pages)
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["supplier_id"])
        for i in ids:
            w.writerow([i])
    print(f"✔️  сохранено {len(ids)} продавцов → {a.output}")


if __name__ == "__main__":
    main()
