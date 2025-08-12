import html
import re

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


def pick_name(d: dict) -> str:
    for k in ("name", "imt_name", "object"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def norm_text(html_text: str) -> str:
    cleaned = re.sub(r"</?(p|li|br|ul|ol)[^>]*>", "\n", html_text or "", flags=re.I)
    txt = BeautifulSoup(cleaned, "html.parser").get_text("\n", strip=True)
    txt = re.sub(r"\s+\n", "\n", txt)
    txt = re.sub(r"[ \t]{2,}", " ", txt)
    return html.unescape(txt).strip()


def get_text(url: str) -> str:
    m = re.search(r"/catalog/(\d+)/", url)
    if not m:
        raise ValueError("invalid url")
    nm_id = int(m.group(1))

    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }
    )

    name, desc_html = "", ""
    vol, part = nm_id // 100000, nm_id // 1000

    for host_tpl in (
        "https://basket-{i:02d}.wb.ru/vol{vol}/part{part}/{nm}/info/ru/card.json",
        "https://static-basket-{i:02d}.wb.ru/vol{vol}/part{part}/{nm}/info/ru/card.json",
    ):
        if desc_html:
            break
        for i in range(1, 13):
            try:
                r = s.get(host_tpl.format(i=i, vol=vol, part=part, nm=nm_id), timeout=6)
                if "application/json" not in r.headers.get("Content-Type", ""):
                    continue
                js = r.json()
                name = pick_name(js) or name
                for f in (
                    "descriptionHtml",
                    "descriptionFull",
                    "description",
                    "descriptionShort",
                ):
                    if js.get(f):
                        desc_html = js[f]
                        break
                if desc_html:
                    break
            except Exception:
                continue

    if not desc_html:
        try:
            r = s.get(
                f"https://card.wb.ru/cards/detail?appType=1&curr=rub&nm={nm_id}",
                timeout=6,
            )
            if "application/json" in r.headers.get("Content-Type", ""):
                js = r.json()
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

    text = (name or str(nm_id)) + "\n\n" + norm_text(desc_html or "")
    return text


def main():
    t = get_text(TEST_URL)
    assert all(sn in t for sn in EXPECTED_SNIPPETS), "Expected snippets missing"
    print("OK")


if __name__ == "__main__":
    main()

