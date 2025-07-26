#!/usr/bin/env python3
"""
social_scraper_uc_selenium_final_full.py — Финальная версия
▪ Извлекает ИНН с Wildberries через Selenium
▪ Получает phone/email через API ФНС и zachestnyibiznes.ru
"""
import csv, time, argparse, pathlib, re, sys, random, json
from typing import Dict, Tuple
from bs4 import BeautifulSoup
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc

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

def get_inn(sid: str, chrome_path: str = None) -> str:
    print(f"\n🔍 Открываем: https://www.wildberries.ru/seller/{sid}")
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-dev-shm-usage")
    driver = uc.Chrome(driver_executable_path=None, use_subprocess=True, options=options, browser_executable_path=chrome_path)

    try:
        url = f"https://www.wildberries.ru/seller/{sid}"
        driver.get(url)

        print("🕒 Ожидаем исчезновения лоадера .spinner...")
        WebDriverWait(driver, 30).until_not(EC.presence_of_element_located((By.CSS_SELECTOR, ".spinner")))

        print("🕒 Ожидаем появления info-иконки...")
        info_icon = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "[class^='seller-info__tooltip-toggle']"))
        )
        info_icon.click()

        print("🕒 Ожидаем появления tooltip...")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CLASS_NAME, "tooltip__content")))

        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")
        tooltip = soup.select_one(".tooltip__content")
        if tooltip:
            text = tooltip.get_text(" ", strip=True)
            print(f"💡 Tooltip: {text[:150]}")
            m = INN_RE.search(text)
            if m:
                print(f"📄 ИНН: {m.group(1)}")
                return m.group(1)
            else:
                print("🔴 ИНН не найден")
        else:
            print("🔴 Tooltip не найден")
        return ""

    except Exception as e:
        print(f"❌ Ошибка Selenium/ИНН: {e}")
        with open(f"debug_dom_{sid}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        driver.save_screenshot(f"debug_{sid}.png")
        return ""
    finally:
        driver.quit()

def query_fns(inn:str)->Tuple[str,str]:
    url = f'https://api-fns.ru/api/egr?req={inn}&key=free'
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        j = r.json().get('items',[{}])[0]
        phone = ''
        for blk in j.get('СвКонтактДл', []):
            if 'Телефон' in blk:
                phone = blk['Телефон']; break
        email = (j.get('СвАдресЮЛ') or {}).get('ЭлПочта','')
        return phone, email
    except: return '', ''

def scrape_zcb(inn:str)->Tuple[str,str]:
    url = f'https://zachestnyibiznes.ru/company/{"ip" if len(inn)==12 else "ul"}/{inn}'
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        block = soup.find(class_='contacts')
        if not block: return '', ''
        t = block.get_text(' ', strip=True)
        return (
            norm_phone(PHONE_RE.search(t).group(0)) if PHONE_RE.search(t) else '',
            EMAIL_RE.search(t).group(0) if EMAIL_RE.search(t) else ''
        )
    except: return '', ''

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="raw.csv")
    parser.add_argument("--output", default="socials.csv")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--fns-limit", type=int, default=100)
    parser.add_argument("--chrome-path", help="Path to Chrome executable")
    parser.add_argument("--skip-fns", action="store_true")
    args = parser.parse_args()

    rows = list(csv.DictReader(open(args.input, encoding="utf-8")))
    state_fns = 0
    for row in rows:
        sid = row.get("supplier_id")
        inn = get_inn(sid, chrome_path=args.chrome_path)
        phone = email = ''
        if inn:
            if not args.skip_fns and state_fns < args.fns_limit:
                p,e = query_fns(inn)
                phone = norm_phone(p)
                email = e
                state_fns += 1
                time.sleep(0.3 + random.random()*0.3)
            if not phone and not email:
                phone, email = scrape_zcb(inn)
        row.update({'inn':inn, 'phone':phone, 'email':email})
        time.sleep(args.delay)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n✔️ saved → {args.output}  |  FNS req: {state_fns}")

if __name__ == "__main__":
    main()
