import datetime
import hashlib
import json
import logging
import os
import secrets
import sqlite3

import jwt
import openai
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ============================
# 🔐 Загрузка секретов из env
# ============================

ENV = os.getenv("ENV", "DEV").upper()
PROD = ENV == "PRODUCTION"

# ✅ OpenAI API Key (обязательно: без него переписывание не работает)
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    raise ValueError(
        "❌ OPENAI_API_KEY не установлен. Укажите его в Railway/GitHub Secrets."
    )
client = openai.OpenAI(api_key=OPENAI_KEY)

# ✅ Robokassa Pass1/Pass2 (используются для подписи форм и callback'ов)
PASS1 = os.getenv("ROBOKASSA_PASS1")
if not PASS1:
    logging.warning("ROBOKASSA_PASS1 не установлен: оплата отключена")
    if not PROD:
        PASS1 = "dev-pass1"

LOGIN = os.getenv("ROBOKASSA_LOGIN", "wb6.ru")

# Pass2 для проверки ResultURL
PASS2 = os.getenv("ROBOKASSA_PASS2")
if not PASS2:
    logging.warning("ROBOKASSA_PASS2 не установлен: оплата отключена")
    if not PROD:
        PASS2 = "dev-pass2"

# Формула контрольной суммы платежной формы
CRC_FORMULA = "md5(MerchantLogin:OutSum:InvId:Pass1[:Shp_param=val...])"

# ✅ JWT-секрет (подписывает access-токены на клиенте)
SECRET = os.getenv("JWT_SECRET")
if not SECRET:
    logging.warning("JWT_SECRET не установлен")
    if not PROD:
        SECRET = secrets.token_hex(16)

# in-memory storage for payment tokens and accounts
TOKENS = {}
ACCOUNTS = {}
LOGIN_INDEX = {}

# ── persistent storage for tokens and invoice counter ──
DB_PATH = os.getenv("TOKENS_DB", os.path.join(os.path.dirname(__file__), "tokens.db"))
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.execute("CREATE TABLE IF NOT EXISTS tokens (inv TEXT PRIMARY KEY, token TEXT)")
DB.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
DB.commit()


def store_token(inv: str, token: str):
    with DB:
        DB.execute(
            "INSERT OR REPLACE INTO tokens(inv, token) VALUES(?, ?)", (str(inv), token)
        )


def fetch_token(inv: str) -> str | None:
    cur = DB.execute("SELECT token FROM tokens WHERE inv=?", (str(inv),))
    row = cur.fetchone()
    if row:
        with DB:
            DB.execute("DELETE FROM tokens WHERE inv=?", (str(inv),))
        return row[0]
    return None


def next_inv_id() -> int:
    with DB:
        cur = DB.execute("SELECT value FROM meta WHERE key='last_inv'")
        row = cur.fetchone()
        last = int(row[0]) if row else 2999
        nxt = last + 1
        if nxt > 2147483647:
            nxt = 3000
        DB.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('last_inv', ?)", (str(nxt),)
        )
    return nxt


PRICES = {"1": "1", "15": "199", "60": "499", "200": "999"}


PROMPT = """
Ты — опытный SEO-копирайтер маркетплейса Wildberries.
Тебе дают исходный текст карточки товара.

🔹 ЗАДАЧА  
Сгенерируй:
1) 🔑 Новый продающий заголовок ≤ 100 символов.  
   • В начале главный ключ.  
   • Без точек, кавычек, % и слова «Wildberries».  
2) 🎯 6 буллитов ≤ 120 символов каждый — выгоды для покупателя, без канцелярита.  
   • Используй глагол в начале («Ускоряет…», «Защищает…»).  
   • Избегай повторов слов.  
3) 🗝️ 20 ключевых фраз CSV, релевантных товару — ранжируй от самых частотных к нишевым.  
   • Используй Яндекс.Wordstat логики: сначала высокочастотные, затем средне- и низкочастотные.  
   • Исключи стоп-слова «купить», «скидка», «wildberries», «дёшево».

🔹 ТОН  
Разговорный, живой, без штампов «лучший», «идеальный». Русский язык.

🔹 ФОРМАТ ВЫВОДА — строго валидный JSON, без комментариев:
{
 "title": "…",
 "bullets": ["…","…","…","…","…","…"],
 "keywords": ["k1","k2", … , "k20"]
}

Валидация: не более 100 символов заголовок; ровно 6 буллитов; ровно 20 ключей.
"""

app = FastAPI()

# ── CORS ──────────────────────────────────────
origins = [
    "https://wb6.ru",
    "http://wb6.ru",
    "https://www.wb6.ru",
    "http://www.wb6.ru",
    "https://wb6.vercel.app",
]
extra = os.getenv("CORS_ORIGINS", "")
origins.extend([o.strip() for o in extra.split(",") if o.strip()])

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.options("/rewrite", include_in_schema=False)  # pre-flight
async def _pre():
    return {}


# ── helpers ───────────────────────────────────
def issue(email: str, quota: int):
    return jwt.encode(
        {
            "sub": email,
            "quota": quota,
            "exp": datetime.datetime.utcnow() + datetime.timedelta(days=30),
        },
        SECRET,
        algorithm="HS256",
    )


def verify(tok: str):
    try:
        return jwt.decode(tok, SECRET, algorithms=["HS256"])
    except:
        return None


def send_email(to: str, login: str, password: str):
    logging.info(f"Email to {to}: login={login} password={password}")


def create_account(email: str, quota: int, inv: str):
    login = secrets.token_hex(4)
    password = secrets.token_hex(4)
    ACCOUNTS[email] = {"login": login, "password": password, "quota": quota}
    LOGIN_INDEX[login] = email
    token = issue(email, quota)
    TOKENS[inv] = token
    store_token(inv, token)
    send_email(email, login, password)
    return token


def wb_text(url: str) -> str:
    """Return meta description from a Wildberries product page."""
    try:
        html = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).text
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", {"name": "description"})
        return meta["content"] if meta else url
    except Exception:
        return url


# ── API ───────────────────────────────────────
class Req(BaseModel):
    supplierId: int
    prompt: str


@app.post("/rewrite")
async def rewrite(r: Req, request: Request):
    info = verify(request.headers.get("Authorization", "").replace("Bearer ", ""))
    if not info:
        info = {"sub": "anon", "quota": 3}  # 3 free
    if info["quota"] <= 0:
        return {"error": "NO_CREDITS"}
    prompt = r.prompt.strip()
    if prompt.startswith("http") and "wildberries.ru" in prompt:
        prompt = wb_text(prompt)
    try:
        comp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
    except Exception as e:
        return {"error": str(e)}
    info["quota"] -= 1
    if info["sub"] in ACCOUNTS:
        ACCOUNTS[info["sub"]]["quota"] = info["quota"]
    return {
        "token": jwt.encode(info, SECRET, "HS256"),
        **json.loads(comp.choices[0].message.content),
    }


# Robokassa ResultURL
@app.post("/payhook")
async def payhook(req: Request):
    f = await req.form()
    inv = f.get("InvId") or f.get("InvoiceID")
    # Collect and sort all Shp_* parameters alphabetically for CRC
    shp_params = {k: f[k] for k in f.keys() if k.startswith("Shp_")}
    shp_part = ":".join(f"{k}={shp_params[k]}" for k in sorted(shp_params))

    def norm_sum(v: str) -> str:
        """Normalize Robokassa sum like '1.00' -> '1'"""
        try:
            n = float(v)
        except Exception:
            return v
        if n.is_integer():
            return str(int(n))
        return ("%f" % n).rstrip("0").rstrip(".")

    sums = [norm_sum(f["OutSum"]), f["OutSum"]]
    ok = False
    for s in sums:
        crc_str = f"{s}:{inv}:{PASS2}"
        if shp_part:
            crc_str += f":{shp_part}"
        crc = hashlib.md5(crc_str.encode()).hexdigest().upper()
        if crc == f["SignatureValue"].upper():
            ok = True
            break
    if not ok:
        return "bad sign"
    price = str(int(float(f["OutSum"])))
    if price == PRICES["1"]:
        quota = 10
    elif price == PRICES["15"]:
        quota = 15
    elif price == PRICES["60"]:
        quota = 60
    elif price == PRICES["200"]:
        quota = 200
    else:
        return "bad sum"
    email = f.get("Email", "user@wb6")
    create_account(email, quota, inv)
    return "OK"


class LoginReq(BaseModel):
    login: str
    password: str


@app.post("/login")
async def login(r: LoginReq):
    email = LOGIN_INDEX.get(r.login)
    if not email:
        return {"error": "AUTH_FAILED"}
    acc = ACCOUNTS.get(email)
    if acc and acc["password"] == r.password:
        return {"token": issue(email, acc["quota"])}
    return {"error": "AUTH_FAILED"}


@app.get("/next_inv")
async def get_next_inv():
    return {"inv": next_inv_id()}


# Информация для тестовой страницы Robokassa
@app.get("/rkinfo")
async def rkinfo():
    return {"pass1": PASS1, "pass2": PASS2, "crc_formula": CRC_FORMULA}


@app.post("/payform")
async def payform(request: Request):
    data = await request.json()
    plan = str(data.get("plan"))
    email = data.get("email", "")
    price = PRICES.get(plan)
    if not price:
        return {"error": "BAD_PLAN"}
    # Robokassa sometimes expects integer sums without trailing zeros.
    # Normalize the amount to avoid values like "1.00" in the form.
    price = str(int(float(price)))
    inv = next_inv_id()
    desc = f"{plan} rewrite"

    # Collect optional Shp_* parameters, excluding Shp_plan
    shp_params = {
        k: str(v) for k, v in data.items() if k.startswith("Shp_") and k != "Shp_plan"
    }

    shp_part = ":".join(f"{k}={shp_params[k]}" for k in sorted(shp_params))
    crc_str = f"{LOGIN}:{price}:{inv}:{PASS1}"
    if shp_part:
        crc_str += f":{shp_part}"
    sig = hashlib.md5(crc_str.encode()).hexdigest()

    fields = {
        "MerchantLogin": LOGIN,
        "OutSum": price,
        "InvId": inv,
        "SignatureValue": sig,
    }
    fields.update(shp_params)
    if email:
        fields["Email"] = email
    html = [
        '<form method="POST" action="https://auth.robokassa.ru/Merchant/Index.aspx" id="rk">'
    ]
    for k, v in fields.items():
        html.append(f'<input type="hidden" name="{k}" value="{v}">')
    html.append("</form>")
    return {"form": "".join(html)}


@app.get("/paytoken")
async def paytoken(inv: int):
    tok = fetch_token(str(inv))
    if not tok:
        tok = TOKENS.pop(str(inv), None)
    if tok:
        return {"token": tok}
    return {"error": "NOT_READY"}
