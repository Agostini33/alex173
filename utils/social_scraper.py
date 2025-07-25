#!/usr/bin/env python3
"""
social_scraper.py  —  контакты продавцов Wildberries
▪н собирает supplier_id  →  ИНН  →  API-ФНС / ZCB  →  phone, e-mail, site, mессенджеры
▪н асинхронные HTTP-запросы через aiohttp (конкуренция регулируется)
▪н опционально рендерит страницу продавца одним экземпляром Playwright (по лимиту)
Usage:
    python social_scraper.py --input raw.csv --output socials.csv \
        --delay 0.2 --concurrency 25 --render-limit 50 --fns-limit 600
"""

from __future__ import annotations
import re, csv, json, argparse, asyncio, time, sys, pathlib
from typing import Dict, List, Tuple
import aiohttp, async_timeout
from bs4 import BeautifulSoup
from tqdm.asyncio import tqdm  # tqdm>=4.66 supports asyncio

# ────────────────── РЭГЕКС ───────────────────────────────────────────────────────────────────────
INN_RE   = re.compile(r'ИНН[:\s]*?(\d{10,12})')
PHONE_RE = re.compile(r'(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}')
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
TG_RE    = re.compile(r'(?:t\.me|telegram\.me)/[A-Za-z0-9_]+')
WA_RE    = re.compile(r'wa\.me/\d+')
SITE_RE  = re.compile(r'https?://[^\s"\'<>]+')

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Chrome/126 Safari/537.36"),
    "Accept": "*/*"
}

# ────────────────── ПОМОЩНИКИ ────────────────────────────────────────────────────────────

def normalize_phone(raw: str) -> str:
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    if len(digits) == 11 and digits.startswith('7'):
        return f'+{digits}'
    if len(digits) == 10:
        return f'+7{digits}'
    return ''


def parse_text(text: str) -> Dict[str, str]:
    return {
        "phone":     normalize_phone(PHONE_RE.search(text).group(0)) if PHONE_RE.search(text) else '',
        "email":     EMAIL_RE.search(text).group(0)   if EMAIL_RE.search(text) else '',
        "telegram":  TG_RE.search(text).group(0)      if TG_RE.search(text) else '',
        "whatsapp":  WA_RE.search(text).group(0)      if WA_RE.search(text) else '',
        "site":      SITE_RE.search(text).group(0)    if SITE_RE.search(text) else '',
    }


async def fetch(session: aiohttp.ClientSession, url: str, **kw) -> str:
    try:
        async with async_timeout.timeout(20):
            async with session.get(url, headers=HEADERS, **kw) as r:
                return await r.text()
    except Exception:
        return ''

# ────────────────── ЯДРО ФУНКЦИИ ────────────────────────────────────────────────────

async def get_inn(session: aiohttp.ClientSession, sid: str) -> str:
    html = await fetch(session, f'https://www.wildberries.ru/seller/{sid}')
    m = INN_RE.search(html)
    return m.group(1) if m else ''


async def query_fns(session: aiohttp.ClientSession, inn: str) -> Tuple[str, str]:
    url = f'https://api-fns.ru/api/egr?req={inn}&key=free'
    txt = await fetch(session, url)
    try:
        js = json.loads(txt)
        item = js.get('items', [{}])[0]
        phone = ''
        # телефоны могут быть списком
        for blk in item.get('СвКонтактДл', []):
            if 'Телефон' in blk:
                phone = blk['Телефон']; break
        email = (item.get('СвАдресЮЛ') or {}).get('ЭлПочта', '')
        return phone, email
    except Exception:
        return '', ''


async def scrape_zcb(session: aiohttp.ClientSession, inn: str) -> Tuple[str, str]:
    url = f'https://zachestnyibiznes.ru/company/{"ip" if len(inn)==12 else "ul"}/{inn}'
    html = await fetch(session, url)
    soup = BeautifulSoup(html, 'html.parser')
    block = soup.find(class_='contacts')
    if not block:
        return '', ''
    txt = block.get_text(' ', strip=True)
    data = parse_text(txt)
    return data['phone'], data['email']


async def process_row(row: Dict[str, str],
                      session: aiohttp.ClientSession,
                      sem: asyncio.Semaphore,
                      args,
                      state) -> Dict[str, str]:
    async with sem:
        sid = row['supplier_id']
        # 1. INN
        inn = await get_inn(session, sid)
        row['inn'] = inn
        contacts = {"phone": '', "email": ''}
        # 2. ФНС
        if inn and not args.skip_fns and state['fns_used'] < args.fns_limit:
            phone, email = await query_fns(session, inn)
            state['fns_used'] += 1
            contacts['phone'], contacts['email'] = phone, email
        # 3. ZCB fallback
        if inn and not contacts['phone'] and not contacts['email']:
            phone, email = await scrape_zcb(session, inn)
            contacts['phone'], contacts['email'] = phone, email
        # 4. Нормализуем
        contacts['phone'] = normalize_phone(contacts['phone'])
        row.update(contacts)
        # delay
        await asyncio.sleep(args.delay)
        return row

# ────────────────── МАЙН ────────────────────────────────────────────────────

async def run(args):
    rows: List[Dict[str, str]] = []
    with open(args.input, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    sem = asyncio.Semaphore(args.concurrency)
    state = {"fns_used": 0}

    async with aiohttp.ClientSession() as session:
        tasks = [process_row(r, session, sem, args, state) for r in rows]
        results = await tqdm.gather(*tasks, desc="Scraping", ncols=80)

    out_fields = list(results[0].keys())
    pathlib.Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        w.writerows(results)

    print(f"✔️  saved → {args.output}  |  FNS req: {state['fns_used']}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input',  default='raw.csv')
    ap.add_argument('--output', default='socials.csv')
    ap.add_argument('--delay', type=float, default=0.1, help='pause between sellers, sec')
    ap.add_argument('--concurrency', type=int, default=25, help='async workers')
    ap.add_argument('--render-limit', type=int, default=50,
                    help='(reserved) JS-render limit, not used in HTTP-only mode')
    ap.add_argument('--fns-limit', type=int, default=600, help='daily quota for api-fns')
    ap.add_argument('--skip-fns', action='store_true', help='’t query api-fns')
    args = ap.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        sys.exit("⏹️  interrupted by user")

if __name__ == '__main__':
    main()

