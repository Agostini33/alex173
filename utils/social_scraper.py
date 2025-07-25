#!/usr/bin/env python3
"""
social_scraper.py  —  WB contacts v4
▪️ Cloudflare-bypass (aiocfscrape) → достаём ИНН из seller-страницы
▪️ API-ФНС / zachestnyibiznes.ru → phone/e-mail
Usage:
  python social_scraper.py --input raw.csv --output socials.csv \
         --delay 0.25 --concurrency 30 --fns-limit 500
"""
from __future__ import annotations
import re, csv, json, argparse, asyncio, sys, pathlib, random
from typing import Dict, List, Tuple

import aiohttp, async_timeout, aiocfscrape
from bs4 import BeautifulSoup
from tqdm.asyncio import tqdm

# ─── REGEX ───────────────────────────────────────────────────────────────────
INN_RE   = re.compile(r'ИНН[:\s]*?(\d{10,12})')
PHONE_RE = re.compile(r'(?:\+7|8)\s*\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}')
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')

HEADERS = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0 Safari/537.36")}

def norm_phone(raw:str)->str:
    digits = re.sub(r'\D', '', raw)
    if len(digits)==11 and digits.startswith('8'):
        digits = '7'+digits[1:]
    if len(digits)==11 and digits.startswith('7'):
        return f'+{digits}'
    if len(digits)==10:
        return f'+7{digits}'
    return ''

async def fetch(session:aiohttp.ClientSession, url:str, timeout:int=15)->str:
    try:
        async with async_timeout.timeout(timeout):
            async with session.get(url, headers=HEADERS) as r:
                return await r.text()
    except Exception:
        return ''

# ─── CORE ────────────────────────────────────────────────────────────────────
async def get_inn_cf(sid:str, scraper)->str:
    url = f'https://www.wildberries.ru/seller/{sid}'
    try:
        html = await (await scraper.get(url, headers=HEADERS, timeout=15)).text()
        m = INN_RE.search(html)
        return m.group(1) if m else ''
    except Exception:
        return ''

async def query_fns(session, inn:str)->Tuple[str,str]:
    url = f'https://api-fns.ru/api/egr?req={inn}&key=free'
    txt = await fetch(session, url, timeout=20)
    try:
        j = json.loads(txt).get('items',[{}])[0]
        phone = ''
        for blk in j.get('СвКонтактДл', []):
            if 'Телефон' in blk:
                phone = blk['Телефон']; break
        email = (j.get('СвАдресЮЛ') or {}).get('ЭлПочта','')
        return phone, email
    except Exception:
        return '', ''

async def scrape_zcb(session, inn:str)->Tuple[str,str]:
    url = f'https://zachestnyibiznes.ru/company/{"ip" if len(inn)==12 else "ul"}/{inn}'
    html = await fetch(session, url)
    soup = BeautifulSoup(html, 'html.parser')
    block = soup.find(class_='contacts')
    if not block: return '', ''
    t = block.get_text(' ', strip=True)
    return (norm_phone(PHONE_RE.search(t).group(0)) if PHONE_RE.search(t) else '',
            EMAIL_RE.search(t).group(0) if EMAIL_RE.search(t) else '')

async def process(row:Dict[str,str], session, scraper, sem, args, state)->Dict[str,str]:
    async with sem:
        sid = row['supplier_id']
        inn  = await get_inn_cf(sid, scraper)
        phone = email = ''
        if inn and not args.skip_fns and state['fns'] < args.fns_limit:
            p,e = await query_fns(session, inn)
            phone = norm_phone(p)
            email = e
            state['fns'] += 1
            await asyncio.sleep(0.25 + random.random()*0.25)   # ФНС не любит «заливку»
        if inn and not phone and not email:
            phone, email = await scrape_zcb(session, inn)
        row.update({'inn':inn,'phone':phone,'email':email})
        await asyncio.sleep(args.delay)
        return row

async def run(args):
    rows = list(csv.DictReader(open(args.input, newline='', encoding='utf-8')))
    sem = asyncio.Semaphore(args.concurrency)
    state = {'fns':0}

    async with aiohttp.ClientSession() as session, await aiocfscrape.create_scraper() as scraper:
        tasks = [process(r, session, scraper, sem, args, state) for r in rows]
        done  = await tqdm.gather(*tasks, ncols=80, desc='Scraping')

    out = args.output
    pathlib.Path(out).parent.mkdir(exist_ok=True, parents=True)
    with open(out,'w',newline='',encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=done[0].keys())
        w.writeheader(); w.writerows(done)
    print(f'✔️ saved → {out}  |  FNS req: {state["fns"]}')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', default='raw.csv')
    ap.add_argument('--output', default='socials.csv')
    ap.add_argument('--delay', type=float, default=0.1)
    ap.add_argument('--concurrency', type=int, default=30)
    ap.add_argument('--fns-limit', type=int, default=600)
    ap.add_argument('--skip-fns', action='store_true')
    args = ap.parse_args()
    try: asyncio.run(run(args))
    except KeyboardInterrupt: sys.exit('⏹️ interrupted')
if __name__ == '__main__':
    main()
