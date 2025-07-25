#!/usr/bin/env python3
import re
import json
import requests
import bs4
import tqdm
import time
import csv
import argparse

INN_RE = re.compile(r'ИНН[:\s]*?(\d{10})')
PHONE_RE = re.compile(r'(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}')
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')


HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux) Chrome/126 Safari/537.36"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.timeout = 10


def get_inn(sid: str) -> str:
    url = f'https://www.wildberries.ru/seller/{sid}'
    html = SESSION.get(url, timeout=10).text
    m = INN_RE.search(html)
    return m.group(1) if m else ''


def query_fns(inn: str) -> tuple[str, str]:
    try:
        r = SESSION.get(f'https://api-fns.ru/api/egr?req={inn}&key=free', timeout=12)
        js = r.json()
        j = js.get('items', [{}])[0]
        phone = email = ''
        for blk in j.get('СвКонтактДл', []):
            if 'Телефон' in blk:
                phone = blk['Телефон']
                break
        adr = j.get('СвАдресЮЛ') or {}
        email = adr.get('ЭлПочта', '')
        return phone, email
    except Exception:
        return '', ''


def scrape_zcb(inn: str) -> tuple[str, str]:
    try:
        url = f'https://zachestnyibiznes.ru/company/ul/{inn}'
        html = SESSION.get(url, timeout=10).text
        soup = bs4.BeautifulSoup(html, 'html.parser')
        div = soup.find('div', class_='contacts')
        text = div.get_text(' ', strip=True) if div else ''
        phone = ''
        email = ''
        m = PHONE_RE.search(text)
        if m:
            phone = m.group(0)
        m = EMAIL_RE.search(text)
        if m:
            email = m.group(0)
        return phone, email
    except Exception:
        return '', ''


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', default='raw_sellers.csv')
    ap.add_argument('--output', default='socials.csv')
    ap.add_argument('--skip-fns', action='store_true')
    args = ap.parse_args()

    with open(args.input, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    for col in ['inn', 'email', 'phone', 'telegram', 'whatsapp', 'site']:
        if col not in fieldnames:
            fieldnames.append(col)

    for row in tqdm.tqdm(rows):
        inn = get_inn(row.get('supplier_id', ''))
        phone = email = ''
        if inn:
            if not args.skip_fns:
                phone, email = query_fns(inn)
            if not (phone or email):
                phone, email = scrape_zcb(inn)
            if phone:
                phone = '+7' + re.sub(r'\D', '', phone)[-10:]
        row['inn'] = inn
        row['email'] = email
        row['phone'] = phone
        row.setdefault('telegram', '')
        row.setdefault('whatsapp', '')
        row.setdefault('site', '')
        time.sleep(0.1)

    with open(args.output, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print('Done:', args.output)


if __name__ == '__main__':
    main()
