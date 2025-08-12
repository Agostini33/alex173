import re
import html
import requests
from bs4 import BeautifulSoup

TEST_URL = "https://www.wildberries.ru/catalog/18488530/detail.aspx"
EXPECTED_SNIPPETS = [
    "Зубная паста Rasyan",
    "натуральная тайская паста",
    "Препятствует образованию зубного камня",
    "Гвоздичное масло",
    "Способ применения:"
]


def get_wb_description(url: str) -> str:
    m = re.search(r"/catalog/(\d+)/", url)
    if not m:
        raise ValueError("invalid url")
    nm_id = int(m.group(1))
    name = ""
    desc_html = ""
    fields = ["descriptionHtml", "description", "descriptionFull", "descriptionShort"]

    # wbx-content-v2
    try:
        js = requests.get(
            f"https://wbx-content-v2.wbstatic.net/ru/{nm_id}.json", timeout=6
        ).json()
        for k in ["name", "imt_name", "object"]:
            if js.get(k):
                name = js[k]
                break
        for f in fields:
            if js.get(f):
                desc_html = js[f]
                break
    except Exception:
        pass

    # card.wb.ru fallback
    if len(desc_html) < 50:
        try:
            api = f"https://card.wb.ru/cards/detail?appType=1&curr=rub&nm={nm_id}"
            js = requests.get(api, timeout=6).json()
            prods = js.get("data", {}).get("products", [])
            prod = None
            for p in prods:
                if p.get("id") == nm_id or p.get("root") == nm_id:
                    prod = p
                    break
            if not prod and prods:
                prod = prods[0]
            if prod:
                name = prod.get("name") or prod.get("imt_name") or name
                if prod.get("description"):
                    desc_html = prod["description"]
        except Exception:
            pass

    # static-basket fallback
    if len(desc_html) < 50:
        vol = nm_id // 100000
        part = nm_id // 1000
        try:
            url3 = (
                f"https://static-basket-01.wb.ru/vol{vol}/part{part}/{nm_id}/info/ru/card.json"
            )
            js = requests.get(url3, timeout=6).json()
            name = js.get("imt_name") or js.get("name") or name
            for f in fields:
                if js.get(f):
                    desc_html = js[f]
                    break
        except Exception:
            pass

    cleaned = re.sub(r"</?(br|li|p|ul|ol)[^>]*>", "\n", desc_html, flags=re.I)
    text = BeautifulSoup(cleaned, "html.parser").get_text("\n")
    text = re.sub(r"\n{2,}", "\n", html.unescape(text)).strip()
    return (name or str(nm_id)) + "\n\n" + text


def main() -> None:
    text = get_wb_description(TEST_URL)
    assert all(sn in text for sn in EXPECTED_SNIPPETS)
    print("OK")


if __name__ == "__main__":
    main()
