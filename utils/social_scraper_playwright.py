#!/usr/bin/env python3
"""
social_scraper.py — WB contacts v5
▪️ Playwright + headless Chromium → достаём ИНН со страницы продавца
▪️ API-ФНС / zachestnyibiznes.ru → phone, email
"""
import re, csv, json, asyncio, argparse, pathlib, sys, random
from typing import Dict, Tuple
from tqdm.asyncio import tqdm
import aiohttp, async_timeout
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

INN_RE   = re.compile(r'ИНН[:\s]*?(\d{10,12})')
PHONE_RE = re.compile(r'(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}')
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) Safari/537.36"}

def norm_phone(raw:str)->str:
    digits = re.sub(r'\D', '', raw)
    if len(digits)==11 and digits.startswith('8'): digits = '7'+digits[1:]
    if len(digits)==11 and digits.startswith('7'): return f'+{digits}'
    if len(digits)==10: return f'+7{digits}'
    return ''

async def fetch(session, url:str, timeout=15)->str:
    try:
        async with async_timeout.timeout(timeout):
            async with session.get(url, headers=HEADERS) as r:
                return await r.text()
    except: return ''

async def get_inn_playwright(page, sid: str) -> str:
    url = f'https://www.wildberries.ru/seller/{sid}'
    try:
        print(f"🔍 Открываем: {url}")

        await page.set_extra_http_headers({
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        })

        await page.add_init_script(
            """Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"""
        )

        await page.goto(url, timeout=30000, wait_until="domcontentloaded")

        # Небольшой «человеческий» скролл
        await page.mouse.wheel(0, 500)
        await page.wait_for_timeout(2000)

        html = await page.content()
        print(f"🟢 DOM WB (SID={sid})[:1000]:\n{html[:1000]}\n")

        m = INN_RE.search(html)
        if m:
            print(f"✅ Найден ИНН: {m.group(1)}")
        else:
            print(f"🔴 ИНН не найден")
        return m.group(1) if m else ''
    except Exception as e:
        print(f"❌ Ошибка Playwright: {e}")
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
    except: return '', ''

async def scrape_zcb(session, inn:str)->Tuple[str,str]:
    url = f'https://zachestnyibiznes.ru/company/{"ip" if len(inn)==12 else "ul"}/{inn}'
    html = await fetch(session, url)
    soup = BeautifulSoup(html, 'html.parser')
    block = soup.find(class_='contacts')
    if not block: return '', ''
    t = block.get_text(' ', strip=True)
    return (norm_phone(PHONE_RE.search(t).group(0)) if PHONE_RE.search(t) else '',
            EMAIL_RE.search(t).group(0) if EMAIL_RE.search(t) else '')

async def process(row:Dict[str,str], page, session, sem, args, state)->Dict[str,str]:
    async with sem:
        sid = row['supplier_id']
        inn = await get_inn_playwright(page, sid)
        phone = email = ''
        if inn and not args.skip_fns and state['fns'] < args.fns_limit:
            p,e = await query_fns(session, inn)
            phone = norm_phone(p)
            email = e
            state['fns'] += 1
            await asyncio.sleep(0.2 + random.random()*0.2)
        if inn and not phone and not email:
            phone, email = await scrape_zcb(session, inn)
        row.update({'inn':inn, 'phone':phone, 'email':email})
        await asyncio.sleep(args.delay)
        return row

async def run(args):
    rows = list(csv.DictReader(open(args.input, newline='', encoding='utf-8')))
    sem = asyncio.Semaphore(args.concurrency)
    state = {'fns':0}

    async with aiohttp.ClientSession() as session:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            tasks = [process(r, page, session, sem, args, state) for r in rows]
            done = await tqdm.gather(*tasks, ncols=80, desc='Scraping')
            await browser.close()

    if not done:
        print("❌ Нет обработанных данных.")
        return
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
    ap.add_argument('--concurrency', type=int, default=20)
    ap.add_argument('--fns-limit', type=int, default=600)
    ap.add_argument('--skip-fns', action='store_true')
    args = ap.parse_args()
    try: asyncio.run(run(args))
    except KeyboardInterrupt: sys.exit('⏹️ interrupted')

if __name__ == '__main__':
    main()
