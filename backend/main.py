import datetime
import hashlib
import html
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
from urllib.parse import quote as _urlquote

import jwt
import openai
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- базовое логирование настраиваемо через ENV ---
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)

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
# Модель по умолчанию — GPT-5; можно переопределить через env
MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
# Фолбэк-модель на случай недоступности основной
MODEL_FALLBACK = os.getenv("OPENAI_MODEL_FALLBACK", "gpt-4o-mini")
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "30"))
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "600"))
WB_UA = os.getenv("WB_UA", "Mozilla/5.0")
EXPOSE_MODEL_ERRORS = os.getenv("EXPOSE_MODEL_ERRORS", "0") == "1"

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
# По умолчанию используем постоянный том Railway, смонтированный в /data
DATA_DIR = os.getenv("DATA_DIR", "/data").rstrip("/")
os.makedirs(DATA_DIR, exist_ok=True)

# Старый путь (рядом с кодом) — на случай миграции
_LEGACY_DB = os.path.join(os.path.dirname(__file__), "tokens.db")

# Новый путь (персистентный)
DB_PATH = os.getenv("TOKENS_DB", os.path.join(DATA_DIR, "tokens.db"))

# Если новый файл ещё не создан, а старый существует — перенесём, чтобы не потерять счётчик
if not os.path.exists(DB_PATH) and os.path.exists(_LEGACY_DB):
    try:
        shutil.copy2(_LEGACY_DB, DB_PATH)
    except Exception:
        pass

# Открываем SQLite с таймаутом и включаем WAL для устойчивости к конкуренции
DB = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
DB.execute("PRAGMA journal_mode=WAL;")
DB.execute("PRAGMA synchronous=NORMAL;")
DB.execute("PRAGMA busy_timeout=5000;")  # мс
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
    """
    Атомарно увеличивает счётчик last_inv и возвращает новое значение.
    Хранится в таблице meta (key='last_inv'), тип value — TEXT, но пишем числа.
    Стартовое значение берётся из ENV INV_START (дефолт 3000).
    """
    INV_START = int(os.getenv("INV_START", "3000"))
    with DB:
        # Инициализация (однократно): если ключа нет — создаём со стартом (INV_START-1), чтобы после инкремента получить INV_START
        DB.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('last_inv', ?)",
            (str(INV_START - 1),),
        )
        # Атомарный инкремент
        DB.execute(
            "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='last_inv'"
        )
        cur = DB.execute("SELECT value FROM meta WHERE key='last_inv'")
        row = cur.fetchone()
        nxt = int(row[0]) if row else INV_START
        # Ограничим верхнюю границу 32-битным int (Robokassa нормально живёт с бОльшими, но оставим поведение)
        if nxt > 2_147_483_647:
            nxt = INV_START
            DB.execute("UPDATE meta SET value = ? WHERE key='last_inv'", (str(nxt),))
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


def wb_card_text(url: str, keep_html: bool = False) -> str:
    """
    Возвращает *имя + подробное описание* товара WB.
    Источники по порядку:
      1) basket-{01..12}.wb.ru
      2) static-basket-{01..12}.wb.ru
      3) card.wb.ru (строго по id/root == nmID и непустому description)
    """
    m = re.search(r"/catalog/(\d+)/", url)
    if not m:
        return url
    nm_id = int(m.group(1))

    def _pick_name(d: dict) -> str:
        for k in ("name", "imt_name", "object"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    JSON_FIELDS = [
        "descriptionHtml",
        "descriptionFull",
        "description",
        "descriptionShort",
    ]

    s = requests.Session()
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    s.headers.update({"User-Agent": ua, "Accept": "application/json"})

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
                name = _pick_name(js) or name
                for f in JSON_FIELDS:
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
                    name = _pick_name(prod) or name
                    desc_html = prod.get("description") or desc_html
        except Exception:
            pass

    cleaned = re.sub(r"</?(p|li|br|ul|ol)[^>]*>", "\n", desc_html or "", flags=re.I)
    text = BeautifulSoup(cleaned, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", html.unescape(text)).strip()

    if len(text) < 60:
        return url

    if keep_html:
        return (name + "\n\n" + (desc_html or "")).strip()

    return (name + "\n\n" + text).strip()


# --- утилита: безопасный парсинг JSON из ответа модели ---
def _extract_json(s: str):
    try:
        return json.loads(s)
    except Exception:
        pass
    # 1) Попробовать вытащить из ```json ... ```
    try:
        m = re.search(r"```(?:json)?\s*({[\s\S]*?})\s*```", s, flags=re.I)
        if m:
            d = json.loads(m.group(1))
            if _schema_ok(d):
                return d
    except Exception:
        pass
    # 2) Сбалансированный поиск первого JSON-объекта
    try:
        start = s.find("{")
        if start != -1:
            depth = 0
            for i, ch in enumerate(s[start:], start=start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        d = json.loads(s[start:i+1])
                        if _schema_ok(d):
                            return d
                        break
    except Exception:
        pass
    return None


def _schema_ok(d):
    if not isinstance(d, dict):
        return False
    b = d.get("bullets"); k = d.get("keywords")
    if not isinstance(b, list) or not isinstance(k, list):
        return False
    if len(b) != 6 or len(k) != 20:
        return False
    return True


def _uses_max_completion_tokens(model_name: str) -> bool:
    """
    Эвристика: модели reasoning-поколения (gpt-5, gpt-4.1, o-серия)
    в Chat Completions ожидают max_completion_tokens вместо max_tokens.
    """
    m = (model_name or "").lower()
    return (
        m.startswith("gpt-5")
        or m.startswith("gpt-4.1")
        or m.startswith("o1")
        or m.startswith("o3")
        or m.startswith("o4")
    )


def _is_json_mode_unsupported(err: Exception) -> bool:
    """Грубая эвристика: модель не поддерживает response_format/json_object."""
    t = str(err).lower()
    keys = ["response_format", "json", "not support", "unsupported", "does not support"]
    return any(k in t for k in keys)


# --- утилита: безопасный вызов OpenAI ---
def _openai_chat(messages, model, max_tokens=OPENAI_MAX_TOKENS, json_mode: bool = True):
    """
    Универсальный вызов chat.completions:
     - если есть .with_options(timeout=...), используем его;
     - иначе пробуем передать timeout прямо в .create();
     - если и это не поддерживается (старый SDK) — вызываем без тайм-аута.
    Всегда просим строгий JSON.
    """
    kwargs = dict(
        model=model,
        messages=messages,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    # Правильное имя параметра лимита токенов для конкретной модели
    if _uses_max_completion_tokens(model):
        kwargs["max_completion_tokens"] = max_tokens
    else:
        kwargs["max_tokens"] = max_tokens
    with_opts = getattr(client.chat.completions, "with_options", None)
    if callable(with_opts):
        return with_opts(timeout=OPENAI_TIMEOUT).create(**kwargs)
    try:
        return client.chat.completions.create(timeout=OPENAI_TIMEOUT, **kwargs)
    except TypeError:
        return client.chat.completions.create(**kwargs)


# --- Диагностика источников WB (без влияния на основную логику) ---
@app.get("/wbtest")
async def wbtest(nm: int = 18488530):
    nm_id = int(nm)
    vol, part = nm_id // 100000, nm_id // 1000
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": WB_UA,
            "Accept": "application/json",
            "Accept-Language": "ru,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "close",
        }
    )

    def try_url(u: str):
        try:
            r = s.get(u, timeout=8, allow_redirects=True)
            ctype = r.headers.get("Content-Type", "")
            ok_json = r.ok and ("application/json" in ctype)
            length = int(r.headers.get("Content-Length") or 0) or len(r.content or b"")
            return {
                "url": u,
                "status": r.status_code,
                "ctype": ctype,
                "ok_json": ok_json,
                "len": length,
            }
        except Exception as e:
            return {"url": u, "error": str(e)}

    results = []
    for host_tpl in (
        "https://basket-{i:02d}.wb.ru/vol{vol}/part{part}/{nm}/info/ru/card.json",
        "https://static-basket-{i:02d}.wb.ru/vol{vol}/part{part}/{nm}/info/ru/card.json",
    ):
        for i in range(1, 13):
            u = host_tpl.format(i=i, vol=vol, part=part, nm=nm_id)
            results.append(try_url(u))

    card = f"https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&spp=0&nm={nm_id}"
    results.append(try_url(card))

    hits = [r for r in results if r.get("ok_json")]
    return {
        "nm": nm_id,
        "hits": hits,
        "results": results,
    }


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
        fetched = wb_card_text(prompt)
        if fetched == prompt or len(fetched) < 60:
            return {
                "error": "WB_FETCH_FAILED",
                "hint": "Попробуйте позже или укажите WB_COOKIES/WB_UA.",
            }
        prompt = fetched
    model_flow = []
    try:
        comp = _openai_chat(
            messages=[
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=MODEL,
            max_tokens=OPENAI_MAX_TOKENS,
            json_mode=True,
        )
        model_flow.append({"model": MODEL, "mode": "json"})
    except Exception as e1:
        logging.error("GEN primary (%s) failed: %s", MODEL, e1)
        if _is_json_mode_unsupported(e1):
            try:
                comp = _openai_chat(
                    messages=[
                        {"role": "system", "content": PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    model=MODEL,
                    max_tokens=OPENAI_MAX_TOKENS,
                    json_mode=False,
                )
                model_flow.append({"model": MODEL, "mode": "nojson"})
            except Exception as e1b:
                logging.error("GEN primary-nojson (%s) failed: %s", MODEL, e1b)
                e1 = e1b
        if not model_flow:
            pass
        try:
            if not model_flow or comp is None:
                comp = _openai_chat(
                    messages=[
                        {"role": "system", "content": PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    model=MODEL_FALLBACK,
                    max_tokens=OPENAI_MAX_TOKENS,
                    json_mode=True,
                )
                model_flow.append({"model": MODEL_FALLBACK, "mode": "json"})
        except Exception as e2:
            logging.error("GEN fallback (%s) failed: %s", MODEL_FALLBACK, e2)
            resp = {
                "error": f"GEN_FAIL: {type(e2).__name__}: {e2}",
                "model_tried": [MODEL, MODEL_FALLBACK],
                "model_flow": model_flow,
            }
            if EXPOSE_MODEL_ERRORS:
                resp["model_error"] = {"primary": str(e1), "fallback": str(e2)}
            return resp
    used_model = getattr(comp, "model", MODEL)
    raw = comp.choices[0].message.content or ""
    data = _extract_json(raw)
    if not data:
        try:
            repair = _openai_chat(
                messages=[
                    {
                        "role": "system",
                        "content": "Верни строго валидный JSON по схеме {title, bullets[6], keywords[20]} без комментариев и пояснений.",
                    },
                    {"role": "user", "content": raw[:8000]},
                ],
                model=MODEL_FALLBACK,
                max_tokens=OPENAI_MAX_TOKENS,
                json_mode=True,
            )
            data = _extract_json(repair.choices[0].message.content or "")
            if data:
                used_model = getattr(repair, "model", used_model)
                model_flow.append({"model": used_model, "mode": "repair"})
        except Exception as e3:
            logging.warning("Repair pass failed: %s", e3)

    if not data or not isinstance(data, dict):
        logging.error("Bad JSON from model. Raw: %s", raw[:500])
        resp = {"error": "BAD_JSON", "raw": raw[:2000], "model_flow": model_flow}
        if EXPOSE_MODEL_ERRORS:
            resp["model_used"] = used_model
        return resp

    info["quota"] -= 1
    if info["sub"] in ACCOUNTS:
        ACCOUNTS[info["sub"]]["quota"] = info["quota"]

    return {
        "token": jwt.encode(info, SECRET, "HS256"),
        "model_used": used_model,
        "model_flow": model_flow,
        **data,
    }


# --- быстрая диагностика соединения с LLM (без WB) ---
@app.get("/gentest")
async def gentest(
    q: str = "Проверка генерации: сделай JSON {title, bullets[6], keywords[20]} для тестовой строки.",
    model: str | None = None,
    json: int = 1,
):
    try:
        comp = _openai_chat(
            messages=[
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": q},
            ],
            model=(model or MODEL),
            max_tokens=min(OPENAI_MAX_TOKENS, 500),
            json_mode=bool(json),
        )
        raw = comp.choices[0].message.content or ""
        data = _extract_json(raw) or {}
        return {
            "ok": True,
            "model": (model or MODEL),
            "fallback": MODEL_FALLBACK,
            "json_mode": bool(json),
            "data_ok": bool(data),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/models")
async def models(prefix: str = "gpt"):
    """
    Вернуть список доступных моделей в аккаунте (именем). Можно фильтровать по префиксу.
    """
    try:
        out = []
        # openai>=1.45.0
        for m in client.models.list():
            name = getattr(m, "id", None) or getattr(m, "model", None) or ""
            if not name:
                continue
            if not prefix or name.startswith(prefix):
                out.append(name)
        return {"ok": True, "count": len(out), "models": sorted(out)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "model": MODEL, "fallback": MODEL_FALLBACK}


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
    return {"crc_formula": CRC_FORMULA}


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
