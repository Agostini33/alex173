import html
import json
import os
import re
import time

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


def pick_name(d: dict) -> str:
    for k in ("name", "imt_name", "object"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def norm_text(html_text: str) -> str:
    cleaned = re.sub(r"</?(br|li|p|ul|ol)[^>]*>", "\n", html_text or "", flags=re.I)
    txt = BeautifulSoup(cleaned, "html.parser").get_text("\n", strip=True)
    txt = re.sub(r"\s+\n", "\n", txt)
    txt = re.sub(r"[ \t]{2,}", " ", txt)
    return html.unescape(txt).strip()


def looks_challenge(t: str) -> bool:
    low = (t or "").lower()
    return any(
        x in low
        for x in ["ddos-guard", "captcha", "__ddginit", "проверка безопасности"]
    )


def get_text(url: str) -> str:
    m = re.search(r"/catalog/(\d+)/", url)
    if not m:
        raise ValueError("invalid url")
    nm_id = int(m.group(1))

    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": os.getenv(
                "WB_UA",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36",
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": os.getenv("WB_LANG", "ru-RU,ru;q=0.9,en-US;q=0.8"),
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Referer": "https://www.wildberries.ru/",
        }
    )
    cookies_env = os.getenv("WB_COOKIES", "")
    for part in cookies_env.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            s.cookies.set(k.strip(), v.strip(), domain=".wildberries.ru")

    name, desc_html = "", ""

    # 1) wbx-content
    try:
        js = s.get(
            f"https://wbx-content-v2.wbstatic.net/ru/{nm_id}.json", timeout=6
        ).json()
        name = pick_name(js) or name
        for f in JSON_FIELDS:
            if js.get(f):
                desc_html = js[f]
                break
    except Exception:
        pass

    # 2) card.wb.ru
    if len(desc_html) < 50:
        try:
            js = s.get(
                f"https://card.wb.ru/cards/detail?appType=1&curr=rub&nm={nm_id}",
                timeout=6,
            ).json()
            prods = js.get("data", {}).get("products", []) or []
            prod = next(
                (
                    p
                    for p in prods
                    if (p.get("id") == nm_id or p.get("root") == nm_id)
                    and p.get("description")
                ),
                None,
            )
            if prod:
                name = pick_name(prod) or name
                desc_html = prod.get("description") or desc_html
        except Exception:
            pass

    # 3) static-basket
    if len(desc_html) < 50:
        vol, part = nm_id // 100000, nm_id // 1000
        for i in range(1, 13):
            try:
                js = s.get(
                    f"https://static-basket-{i:02d}.wb.ru/vol{vol}/part{part}/{nm_id}/info/ru/card.json",
                    timeout=6,
                ).json()
                name = pick_name(js) or name
                for f in JSON_FIELDS:
                    if js.get(f):
                        desc_html = js[f]
                        break
                if len(desc_html) >= 50:
                    break
            except Exception:
                continue

    # 4) HTML
    if len(desc_html) < 50:
        try:
            s.get("https://www.wildberries.ru/", timeout=6)
            time.sleep(0.3)
            r = s.get(url, timeout=8)
            txt = r.text
            tries = 2
            while tries and looks_challenge(txt):
                time.sleep(1.0)
                r = s.get(url, timeout=8)
                txt = r.text
                tries -= 1

            m = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                txt,
                re.S,
            )
            if m:
                try:
                    j = json.loads(m.group(1))
                    prod = (
                        j.get("props", {})
                        .get("pageProps", {})
                        .get("initialState", {})
                        .get("products", {})
                    )
                    name = pick_name(prod) or name
                    desc_html = (
                        prod.get("descriptionFull")
                        or prod.get("description")
                        or desc_html
                    )
                except Exception:
                    pass

            if len(desc_html) < 50:
                soup = BeautifulSoup(txt, "html.parser")
                for tag in soup.find_all("script", {"type": "application/ld+json"}):
                    try:
                        data = json.loads(tag.string or "{}")
                    except Exception:
                        continue
                    if isinstance(data, list):
                        for it in data:
                            if (
                                isinstance(it, dict)
                                and it.get("@type") == "Product"
                                and it.get("description")
                            ):
                                name = it.get("name") or name
                                desc_html = it.get("description")
                                break
                    elif (
                        isinstance(data, dict)
                        and data.get("@type") == "Product"
                        and data.get("description")
                    ):
                        name = data.get("name") or name
                        desc_html = data.get("description")
                    if len(desc_html) >= 50:
                        break
        except Exception:
            pass

    text = (name or str(nm_id)) + "\n\n" + norm_text(desc_html or "")
    return text


def main():
    t = get_text(TEST_URL)
    assert all(sn in t for sn in EXPECTED_SNIPPETS), "Expected snippets missing"
    print("OK")


if __name__ == "__main__":
    main()
