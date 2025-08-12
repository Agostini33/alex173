import html
import json
import os
import re
import time
import typing as t

import requests
from bs4 import BeautifulSoup

TEST_URL = "https://www.wildberries.ru/catalog/18488530/detail.aspx"
EXPECTED_SNIPPETS = [
    "Зубная паста Rasyan",
    "натуральная тайская паста",
    "Препятствует образованию зубного камня",
    "Гвоздичное масло",
    "Способ применения:",
]

JSON_FIELDS = ["descriptionHtml", "descriptionFull", "description", "descriptionShort"]

DEFAULT_UA = os.getenv(
    "WB_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
)


def parse_cookies_str(cookie_str: str) -> dict:
    jar = {}
    if not cookie_str:
        return jar
    for part in cookie_str.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            jar[k.strip()] = v.strip()
    return jar


def norm_text(html_text: str) -> str:
    cleaned = re.sub(r"</?(br|li|p|ul|ol)[^>]*>", "\n", html_text, flags=re.I)
    txt = BeautifulSoup(cleaned, "html.parser").get_text("\n", strip=True)
    txt = re.sub(r"\s+\n", "\n", txt)
    txt = re.sub(r"[ \t]{2,}", " ", txt)
    return html.unescape(txt).strip()


def pick_name(d: dict) -> str:
    for k in ("name", "imt_name", "object"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def get_from_wbx_content(nm_id: int, s: requests.Session) -> t.Tuple[str, str]:
    url = f"https://wbx-content-v2.wbstatic.net/ru/{nm_id}.json"
    r = s.get(url, timeout=6)
    js = r.json()
    name = pick_name(js)
    desc_html = ""
    for f in JSON_FIELDS:
        if js.get(f):
            desc_html = js[f]
            break
    return name, desc_html


def get_from_card_api(nm_id: int, s: requests.Session) -> t.Tuple[str, str]:
    url = f"https://card.wb.ru/cards/detail?appType=1&curr=rub&nm={nm_id}"
    js = s.get(url, timeout=6).json()
    prods = js.get("data", {}).get("products", []) or []
    # искать ровно наш id/root и непустое описание
    prod = next(
        (
            p
            for p in prods
            if (p.get("id") == nm_id or p.get("root") == nm_id) and p.get("description")
        ),
        None,
    )
    if not prod and prods:
        prod = prods[0]
    if not prod:
        return "", ""
    return pick_name(prod), prod.get("description") or ""


def get_from_static_basket(nm_id: int, s: requests.Session) -> t.Tuple[str, str]:
    vol = nm_id // 100000
    part = nm_id // 1000
    url = (
        f"https://static-basket-01.wb.ru/vol{vol}/part{part}/{nm_id}/info/ru/card.json"
    )
    js = s.get(url, timeout=6).json()
    name = pick_name(js)
    desc_html = ""
    for f in JSON_FIELDS:
        if js.get(f):
            desc_html = js[f]
            break
    return name, desc_html


def looks_like_challenge(text: str) -> bool:
    markers = [
        "DDOS-GUARD",
        "DDoS-Guard",
        "Access denied",
        "captcha",
        "__ddginit",
        "cf-chl",
        "Проверка безопасности",
        "Проверяем ваш браузер",
    ]
    low = text.lower()
    return any(m.lower() in low for m in markers)


def get_from_html(url: str, s: requests.Session) -> t.Tuple[str, str]:
    # прогрев
    s.get("https://www.wildberries.ru/", timeout=6)
    time.sleep(0.4)

    r = s.get(url, timeout=8, allow_redirects=True)
    txt = r.text

    # если пришёл челлендж — пару ретраев
    retries = 2
    while retries and looks_like_challenge(txt):
        time.sleep(1.0)
        r = s.get(url, timeout=8, allow_redirects=True)
        txt = r.text
        retries -= 1

    # 1) __NEXT_DATA__
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', txt, re.S
    )
    if m:
        try:
            js = json.loads(m.group(1))
            prod = (
                js.get("props", {})
                .get("pageProps", {})
                .get("initialState", {})
                .get("products", {})
            )
            name = pick_name(prod) or pick_name({"name": prod.get("name", "")})
            desc_html = prod.get("descriptionFull") or prod.get("description") or ""
            if desc_html:
                return name, desc_html
        except Exception:
            pass

    # 2) ld+json (schema.org/Product)
    for tag in BeautifulSoup(txt, "html.parser").find_all(
        "script", {"type": "application/ld+json"}
    ):
        try:
            data = json.loads(tag.string or "{}")
        except Exception:
            continue
        # иногда массив
        if isinstance(data, list):
            for it in data:
                if isinstance(it, dict) and it.get("@type") == "Product":
                    name = it.get("name") or ""
                    desc = it.get("description") or ""
                    if desc:
                        return name, desc
        elif isinstance(data, dict) and data.get("@type") == "Product":
            name = data.get("name") or ""
            desc = data.get("description") or ""
            if desc:
                return name, desc

    return "", ""


def get_wb_description(url: str) -> str:
    m = re.search(r"/catalog/(\d+)/", url)
    if not m:
        raise ValueError("invalid url")
    nm_id = int(m.group(1))

    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": os.getenv(
                "WB_LANG", "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
            ),
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://www.wildberries.ru/",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    cookies_env = os.getenv("WB_COOKIES", "")
    if cookies_env:
        s.cookies.update(parse_cookies_str(cookies_env))

    # 1) JSON-источники
    getters = [
        lambda: get_from_wbx_content(nm_id, s),
        lambda: get_from_card_api(nm_id, s),
        lambda: get_from_static_basket(nm_id, s),
    ]
    name, desc_html = "", ""
    for fn in getters:
        try:
            name, desc_html = fn()
            if len(desc_html or "") >= 50:
                break
        except Exception:
            continue

    # 2) HTML-фоллбек
    if len(desc_html or "") < 50:
        name2, desc2 = get_from_html(url, s)
        if len(desc2 or "") >= 50:
            name = name or name2
            desc_html = desc2

    # итог
    text = (name or str(nm_id)) + "\n\n" + norm_text(desc_html or "")
    return text.strip()


def main() -> None:
    text = get_wb_description(TEST_URL)
    assert all(sn in text for sn in EXPECTED_SNIPPETS), "Expected snippets missing"
    print("OK")


if __name__ == "__main__":
    main()
