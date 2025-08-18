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
import time
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

# Настройки JSON-режима и лимитов вывода
OPENAI_JSON_MODE = os.getenv("OPENAI_JSON_MODE", "schema").lower()  # off|object|schema
try:
    OPENAI_MAX_OUTPUT_TOKENS = int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "0"))
except Exception:
    OPENAI_MAX_OUTPUT_TOKENS = 0

# Модель по умолчанию — GPT-5; можно переопределить через env
MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
# Фолбэк-модель на случай недоступности основной
MODEL_FALLBACK = os.getenv("OPENAI_MODEL_FALLBACK", "gpt-4o-mini")
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "30"))
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "800"))
EXPOSE_MODEL_ERRORS = os.getenv("EXPOSE_MODEL_ERRORS", "0") == "1"
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "1"))
WB_DEBUG = os.getenv("WB_DEBUG", "0") == "1"
WB_TIMEOUT = float(os.getenv("WB_TIMEOUT", "6.0"))
WB_UA = os.getenv("WB_UA", "Mozilla/5.0")

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
    Источники:
      1) basket-{01..12}.wb.ru (card.json) — проверяем nm_id
      2) static-basket-{01..12}.wb.ru (card.json) — проверяем nm_id
      3) card.wb.ru (id/root == nmID и непустой description)
      4) HTML fallback: __NEXT_DATA__ → descriptionFull|description
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
    tried = []
    used_source = ""
    used_host = ""

    for host_tpl in (
        "https://basket-{i:02d}.wb.ru/vol{vol}/part{part}/{nm}/info/ru/card.json",
        "https://static-basket-{i:02d}.wb.ru/vol{vol}/part{part}/{nm}/info/ru/card.json",
    ):
        if desc_html:
            break
        for i in range(1, 13):
            try:
                h = host_tpl.format(i=i, vol=vol, part=part, nm=nm_id)
                tried.append(h)
                r = s.get(h, timeout=WB_TIMEOUT)
                ct = r.headers.get("Content-Type", "")
                if "application/json" not in ct:
                    continue
                js = r.json()
                if (
                    isinstance(js, dict)
                    and js.get("nm_id")
                    and int(js["nm_id"]) != nm_id
                ):
                    continue
                name = _pick_name(js) or name
                for f in JSON_FIELDS:
                    if js.get(f):
                        desc_html = js[f]
                        break
                if desc_html:
                    used_source = "basket" if "basket-" in h else "static-basket"
                    used_host = h
                    break
            except Exception:
                continue

    if not desc_html:
        try:
            r = s.get(
                f"https://card.wb.ru/cards/detail?appType=1&curr=rub&nm={nm_id}",
                timeout=WB_TIMEOUT,
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
                    used_source, used_host = "card", "card.wb.ru"
        except Exception:
            pass

    # HTML fallback
    if not desc_html:
        try:
            s.headers.update({"Accept": "text/html"})
            r = s.get(url, timeout=WB_TIMEOUT)
            html_text = r.text
            mjs = re.search(
                r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S
            )
            if mjs:
                data = json.loads(mjs.group(1))
                prod = (
                    data.get("props", {})
                    .get("pageProps", {})
                    .get("initialState", {})
                    .get("products", {})
                )
                if isinstance(prod, dict):
                    nm = prod.get("id") or prod.get("root") or nm_id
                    if int(nm) == nm_id:
                        name = prod.get("name") or name
                        desc_html = (
                            prod.get("descriptionFull") or prod.get("description") or ""
                        )
                        if desc_html:
                            used_source, used_host = "html", "wildberries.ru"
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


# ============================
# 🔎 Вспомогательные утилиты
# ============================
_JSON_OBJ = re.compile(r"\{.*\}", re.S)


def _extract_json(s: str):
    if not isinstance(s, str) or "{" not in s:
        return None
    m = _JSON_OBJ.search(s)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _schema_ok(d):
    if not isinstance(d, dict):
        return False
    if not all(k in d for k in ("title", "bullets", "keywords")):
        return False
    if not isinstance(d.get("bullets"), list) or len(d["bullets"]) != 6:
        return False
    if not isinstance(d.get("keywords"), list) or len(d["keywords"]) != 20:
        return False
    return True


def _find_schema_dict(obj, _depth=0):
    """Рекурсивно находит первый dict по нашей схеме во вложенных структурах."""
    if _depth > 6:
        return None
    try:
        if hasattr(obj, "model_dump"):
            obj = obj.model_dump()
    except Exception:
        pass
    if isinstance(obj, dict):
        if _schema_ok(obj):
            return obj
        for v in obj.values():
            x = _find_schema_dict(v, _depth + 1)
            if x:
                return x
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            x = _find_schema_dict(v, _depth + 1)
            if x:
                return x
    return None


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


def _omit_temperature(model_name: str) -> bool:
    """
    Для reasoning-поколения (gpt-5/4.1/o*) Chat Completions не принимает произвольную temperature.
    Оставляем дефолт (1) — т.е. НЕ передаём параметр.
    """
    m = (model_name or "").lower()
    return (
        m.startswith("gpt-5")
        or m.startswith("gpt-4.1")
        or m.startswith("o1")
        or m.startswith("o3")
        or m.startswith("o4")
    )


def _json_response_format(model: str, want: str = OPENAI_JSON_MODE):
    """
    Вернёт dict для response_format или None.
    want = schema|object|off
    """
    if not want or want == "off":
        return None
    if want == "schema":
        # Жёсткая схема: ровно 6 bullets и 20 keywords, title <=100
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "wb6_schema",
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "bullets", "keywords"],
                    "properties": {
                        "title": {"type": "string", "maxLength": 100},
                        "bullets": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 120},
                            "minItems": 6,
                            "maxItems": 6,
                        },
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 20,
                            "maxItems": 20,
                        },
                    },
                },
            },
        }
    # object
    return {"type": "json_object"}


def _msg_to_data_and_raw(msg):
    """
    Возвращает (data_dict_or_None, raw_text).
    Предпочитает структурированный ответ (message.parsed), затем content parts, затем глубокий поиск JSON.
    """
    # 1) structured output (parsed)
    parsed = getattr(msg, "parsed", None)
    if parsed is not None:
        try:
            if hasattr(parsed, "model_dump"):
                d = parsed.model_dump()
            elif hasattr(parsed, "dict"):
                d = parsed.dict()
            elif isinstance(parsed, dict):
                d = parsed
            else:
                d = json.loads(getattr(parsed, "json", lambda: str(parsed))())
        except Exception:
            d = None
        if isinstance(d, dict) and _schema_ok(d):
            return d, json.dumps(d, ensure_ascii=False)
        found = _find_schema_dict(parsed)
        if found:
            return found, json.dumps(found, ensure_ascii=False)

    # 2) content как строка или список частей
    content = getattr(msg, "content", None)
    raw = ""
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        chunks = []
        for part in content:
            try:
                if hasattr(part, "model_dump"):
                    part = part.model_dump()
            except Exception:
                pass
            if isinstance(part, dict):
                ptype = part.get("type")
                if ptype in ("text", "output_text", "reasoning"):
                    t = part.get("text")
                    if isinstance(t, str) and t.strip():
                        chunks.append(t)
                elif ptype == "output_json":
                    js = part.get("json")
                    if isinstance(js, (dict, list)):
                        chunks.append(json.dumps(js, ensure_ascii=False))
            elif isinstance(part, str):
                chunks.append(part)
        raw = "\n".join(chunks).strip()
    else:
        raw = (str(content) if content is not None else "") or ""

    d = _extract_json(raw) or None
    if _schema_ok(d or {}):
        return d, raw
    found = _find_schema_dict(msg)
    if found:
        return found, json.dumps(found, ensure_ascii=False)
    return d, raw


def _shape_digest(obj, maxlen: int = 200):
    """
    Короткий дайджест структуры для логов (когда включён EXPOSE_MODEL_ERRORS).
    """
    try:
        if hasattr(obj, "model_dump"):
            obj = obj.model_dump()
        s = json.dumps(obj, ensure_ascii=False)[:maxlen]
        return s
    except Exception:
        s = str(obj)
        return s[:maxlen] + ("…" if len(s) > maxlen else "")


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
    По возможности просим строгий JSON (json_schema|json_object).
    """
    kwargs = dict(
        model=model,
        messages=messages,
    )
    # Температура: для reasoning-моделей НЕ передаём параметр (используется дефолт=1)
    if not _omit_temperature(model):
        kwargs["temperature"] = OPENAI_TEMPERATURE
    # Поддержка JSON-mode (схема/объект/выключено)
    rf = _json_response_format(model, OPENAI_JSON_MODE) if json_mode else None
    if rf:
        kwargs["response_format"] = rf
    # Правильное имя параметра лимита токенов для конкретной модели
    if _uses_max_completion_tokens(model):
        kwargs["max_completion_tokens"] = max_tokens
    else:
        kwargs["max_tokens"] = max_tokens
    # Если просим строгую схему — сначала попробуем parse()
    if rf and rf.get("type") == "json_schema":
        try:
            # parse() обычно не принимает timeout напрямую; пробуем без with_options
            return client.chat.completions.parse(**kwargs)
        except Exception:
            # продолжим обычным путём ниже
            pass

    with_opts = getattr(client.chat.completions, "with_options", None)
    if callable(with_opts):
        try:
            return with_opts(timeout=OPENAI_TIMEOUT).create(**kwargs)
        except Exception as e:
            # если json_schema не поддержан — фолбэк на json_object
            if rf and rf.get("type") == "json_schema":
                try:
                    kwargs_fallback = dict(kwargs)
                    kwargs_fallback["response_format"] = {"type": "json_object"}
                    return with_opts(timeout=OPENAI_TIMEOUT).create(**kwargs_fallback)
                except Exception:
                    raise
            raise
    # Вариант 2: timeout в create (поддерживается в некоторых версиях)
    try:
        return client.chat.completions.create(timeout=OPENAI_TIMEOUT, **kwargs)
    except TypeError:
        # Вариант 3: совсем без timeout параметра
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            # фолбэк с json_object, если json_schema не поддержан
            if rf and rf.get("type") == "json_schema":
                kwargs_fb = dict(kwargs)
                kwargs_fb["response_format"] = {"type": "json_object"}
                return client.chat.completions.create(**kwargs_fb)
            raise


def _openai_responses(*, messages, model, json_mode: bool):
    """
    Новый путь: Responses API — используем для gpt-5.
    input — это список messages со структурой роли/контента.
    """
    rf = _json_response_format(model, OPENAI_JSON_MODE) if json_mode else None
    opts = getattr(client.responses, "with_options", None)
    kwargs = {"model": model, "input": messages}
    if rf:
        kwargs["response_format"] = rf
    if OPENAI_MAX_OUTPUT_TOKENS > 0:
        kwargs["max_output_tokens"] = OPENAI_MAX_OUTPUT_TOKENS

    def _create_call(kws):
        if callable(opts):
            try:
                return opts(timeout=OPENAI_TIMEOUT).create(**kws)
            except Exception:
                pass
        return client.responses.create(timeout=OPENAI_TIMEOUT, **kws)

    try:
        return _create_call(kwargs)
    except TypeError:
        # Параметр max_output_tokens может не поддерживаться старым SDK
        if "max_output_tokens" in kwargs:
            try:
                kwargs2 = dict(kwargs)
                kwargs2.pop("max_output_tokens", None)
                return _create_call(kwargs2)
            except TypeError:
                pass
        guard = (
            "ВЕРНИ СТРОГО ВАЛИДНЫЙ JSON-ОБЪЕКТ ровно такого вида:\n"
            '{ "title": string, "bullets": [6 strings], "keywords": [20 strings] }\n'
            "Без комментариев, без пояснений, без лишних полей. Только JSON.\n"
            "bullets — ровно 6 строк, keywords — ровно 20 строк."
        )
        guarded = [{"role": "system", "content": guard}] + list(messages or [])
        fallback_kwargs = {"model": model, "input": guarded}
        if OPENAI_MAX_OUTPUT_TOKENS > 0:
            try:
                fallback_kwargs["max_output_tokens"] = OPENAI_MAX_OUTPUT_TOKENS
                return _create_call(fallback_kwargs)
            except TypeError:
                pass
        return _create_call({"model": model, "input": guarded})


def _msg_from_response(resp):
    """
    Унифицируем извлечение 'сообщения' из Responses API под интерфейс _msg_to_data_and_raw.
    Возвращаем объект-пустышку с полем .content = output_text (если он есть),
    а также склеиваем все json/text части.
    """

    class _M:
        def __init__(self, text):
            self.content = text

    raw = ""
    try:
        ot = getattr(resp, "output_text", None)
        if isinstance(ot, str) and ot.strip():
            raw = ot
    except Exception:
        pass
    try:
        out = getattr(resp, "output", None) or []
        chunks = []
        for item in out:
            cont = getattr(item, "content", None) or []
            for part in cont:
                ptype = getattr(part, "type", None)
                if ptype in ("output_text", "text", "reasoning"):
                    t = getattr(getattr(part, "text", None), "value", None)
                    if isinstance(t, str) and t.strip():
                        chunks.append(t)
                elif ptype == "output_json":
                    js = getattr(part, "json", None)
                    if js is not None:
                        try:
                            import json as _json

                            chunks.append(_json.dumps(js, ensure_ascii=False))
                        except Exception:
                            pass
        if chunks and not raw:
            raw = "\n".join(chunks).strip()
    except Exception:
        pass
    return _M(raw or "")


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
    rewriteDescription: bool = False
    stylePrimary: str | None = None
    styleSecondary: str | None = None
    styleCustom: str | None = None


def _desc_instructions(primary: str | None, secondary: str | None, custom: str | None) -> str:
    parts = []
    m = (primary or "").strip().lower()
    if m == "только seo":
        parts.append(
            "сделай чисто SEO-описание: формально, без воды, включай релевантные ключевые фразы естественно"
        )
    elif m == "расширить":
        parts.append("расширь текст примерно на 50%, добавь фактов и выгод")
    elif m == "сократить":
        parts.append("сократи текст примерно на 40%, оставь главное")
    elif m == "казуально, как для друга":
        parts.append("дружелюбный, разговорный тон")
    elif m == "деловой стиль":
        parts.append("официально-деловой тон, на 'Вы'")
    elif m == "для мам":
        parts.append("тёплый, сочувственный тон для мам маленьких детей")
    elif m == "экспертный/технический":
        parts.append("строгий экспертный стиль, терминология, по делу")
    elif m == "нейтрально/сдержанно":
        parts.append("нейтральный, фактический тон, без эмоций")

    s = (secondary or "").strip().lower()
    if s == "структура: aida":
        parts.append("структура AIDA (Attention-Interest-Desire-Action)")
    elif s == "структура: storytelling":
        parts.append("короткий сторителлинг: мини-история о том, как продукт решает проблему")
    elif s == "структура: pain-agitate-solve":
        parts.append("подход Pain-Agitate-Solve")
    elif s == "формат: списком (bullets)":
        parts.append("оформи как маркированный список, 5–8 ёмких пунктов")
    elif s == "формат: сплошным текстом":
        parts.append("один-два абзаца сплошного текста, без списков")
    elif s == "с эмодзи":
        parts.append("вставь 2–4 уместных эмодзи")
    elif s == "без эмодзи":
        parts.append("без эмодзи")

    if custom and custom.strip():
        parts.append(custom.strip())

    parts.append("русский язык; без HTML; без ссылок; не упоминай Wildberries")
    parts.append(
        "не выдумывай факты про состав/сертификацию, если их нет во входных данных"
    )
    return "; ".join(parts)


@app.post("/rewrite")
async def rewrite(r: Req, request: Request):
    info = verify(request.headers.get("Authorization", "").replace("Bearer ", ""))
    if not info:
        info = {"sub": "anon", "quota": 3}  # 3 free
    if info["quota"] <= 0:
        return {"error": "NO_CREDITS"}
    prompt = r.prompt.strip()
    wb_meta = None
    if prompt.startswith("http") and "wildberries.ru" in prompt:
        fetched = wb_card_text(prompt)
        if fetched == prompt or len(fetched) < 60:
            resp = {
                "error": "WB_FETCH_FAILED",
                "hint": "Попробуйте позже или укажите WB_COOKIES/WB_UA.",
            }
            if WB_DEBUG:
                resp["wb_meta"] = {"url": prompt}
            return resp
        prompt = fetched
        if WB_DEBUG:
            wb_meta = {"url": r.prompt, "fetched_len": len(fetched)}
    try:
        t0 = time.monotonic()
        if MODEL.startswith("gpt-5"):
            comp = _openai_responses(
                messages=[
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": prompt},
                ],
                model=MODEL,
                json_mode=True,
            )
            used_model = getattr(comp, "model", MODEL)
            model_flow = [{"model": used_model, "mode": "json"}]
            msg = _msg_from_response(comp)
        else:
            comp = _openai_chat(
                messages=[
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": prompt},
                ],
                model=MODEL,
                max_tokens=OPENAI_MAX_TOKENS,
                json_mode=True,
            )
            used_model = getattr(comp, "model", MODEL)
            model_flow = [{"model": used_model, "mode": "json"}]
            msg = comp.choices[0].message
    except Exception as e:
        return {"error": str(e)}
    data, raw = _msg_to_data_and_raw(msg)
    gen_ms = int((time.monotonic() - t0) * 1000)

    repair_attempted = False
    repair_used = False
    repair_ms = 0
    if not data:
        # "Чинящий" проход на фолбэке — только если есть, что чинить
        repair_attempted = True
        repair_input = (raw or prompt or "").strip()
        if len(repair_input) >= 30:
            rt0 = time.monotonic()
            try:
                if MODEL_FALLBACK.startswith("gpt-5"):
                    repair_resp = _openai_responses(
                        messages=[
                            {
                                "role": "system",
                                "content": "Верни строго валидный JSON по схеме {title, bullets[6], keywords[20]} без комментариев и пояснений.",
                            },
                            {"role": "user", "content": repair_input[:8000]},
                        ],
                        model=MODEL_FALLBACK,
                        json_mode=True,
                    )
                    d2, _raw2 = _msg_to_data_and_raw(_msg_from_response(repair_resp))
                    used_model = getattr(repair_resp, "model", used_model)
                else:
                    repair = _openai_chat(
                        messages=[
                            {
                                "role": "system",
                                "content": "Верни строго валидный JSON по схеме {title, bullets[6], keywords[20]} без комментариев и пояснений.",
                            },
                            {"role": "user", "content": repair_input[:8000]},
                        ],
                        model=MODEL_FALLBACK,
                        max_tokens=OPENAI_MAX_TOKENS,
                        json_mode=True,
                    )
                    d2, _raw2 = _msg_to_data_and_raw(repair.choices[0].message)
                    used_model = getattr(repair, "model", used_model)
                if d2:
                    data = d2
                    model_flow.append({"model": used_model, "mode": "repair"})
                    repair_used = True
            except Exception as e3:
                logging.warning("Repair pass failed: %s", e3)
            repair_ms = int((time.monotonic() - rt0) * 1000)
        else:
            resp = {
                "error": "BAD_JSON_EMPTY",
                "model_flow": model_flow,
                "timings": {"gen_ms": gen_ms, "repair_ms": 0},
            }
            if wb_meta and WB_DEBUG:
                resp["wb_meta"] = wb_meta
            if EXPOSE_MODEL_ERRORS:
                resp["model_used"] = used_model
            return resp

    # Валидация и финальный ответ
    if not data or not _schema_ok(data):
        resp = {
            "error": "BAD_JSON",
            "raw": (raw or "")[:2000],
            "model_flow": model_flow,
            "timings": {"gen_ms": gen_ms, "repair_ms": repair_ms},
            "repair_attempted": repair_attempted,
            "repair_used": repair_used,
        }
        if EXPOSE_MODEL_ERRORS:
            resp["model_used"] = used_model
        if wb_meta and WB_DEBUG:
            resp["wb_meta"] = wb_meta
        return resp

    info["quota"] -= 1
    if info["sub"] in ACCOUNTS:
        ACCOUNTS[info["sub"]]["quota"] = info["quota"]
    out = dict(data)
    if r.rewriteDescription:
        source_text = prompt
        instr = _desc_instructions(r.stylePrimary, r.styleSecondary, r.styleCustom)
        sys = (
            "Ты редактор маркетплейса. Перепиши связное ОПИСАНИЕ товара по инструкциям. Верни ТОЛЬКО текст описания, без пояснений."
        )
        user = f"Инструкции: {instr}\n\nИсходный текст карточки:\n{source_text}"
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        description_text = None
        try:
            if model.startswith("gpt-5"):
                res = client.responses.create(
                    model=model,
                    input=[
                        {"role": "system", "content": sys},
                        {"role": "user", "content": user},
                    ],
                )
                try:
                    description_text = (getattr(res, "output_text", "") or "").strip()
                except Exception:
                    for item in getattr(res, "output", []) or []:
                        t = getattr(item, "content", None)
                        if isinstance(t, str) and t.strip():
                            description_text = t.strip()
                            break
            else:
                cc = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": sys},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.2,
                )
                description_text = (cc.choices[0].message.content or "").strip()
        except Exception:
            description_text = None
        if description_text:
            out["description"] = description_text
    return {
        "token": jwt.encode(info, SECRET, "HS256"),
        "model_used": used_model,
        "model_flow": model_flow,
        "timings": {"gen_ms": gen_ms, "repair_ms": repair_ms},
        "repair_attempted": repair_attempted,
        "repair_used": repair_used,
        **({"wb_meta": wb_meta} if (wb_meta and WB_DEBUG) else {}),
        **out,
    }


# --- быстрая диагностика соединения с LLM (без WB) ---
@app.get("/gentest")
async def gentest(
    q: str = "Проверка генерации: сделай JSON {title, bullets[6], keywords[20]} для тестовой строки.",
    model: str | None = None,
    json: int = 1,
    raw: int = 0,
    diag: int = 0,
):
    t0 = time.monotonic()
    m = model or MODEL
    try:
        if m.startswith("gpt-5"):
            comp = _openai_responses(
                messages=[
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": q},
                ],
                model=m,
                json_mode=bool(json),
            )
            used_model = getattr(comp, "model", m)
            resp_msg = _msg_from_response(comp)
        else:
            comp = _openai_chat(
                messages=[
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": q},
                ],
                model=m,
                max_tokens=OPENAI_MAX_TOKENS,
                json_mode=bool(json),
            )
            used_model = getattr(comp, "model", m)
            resp_msg = comp.choices[0].message
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    msg = resp_msg
    data, raw_text = _msg_to_data_and_raw(msg)
    gen_ms = int((time.monotonic() - t0) * 1000)
    resp = {
        "ok": True,
        "model": used_model,
        "fallback": MODEL_FALLBACK,
        "json_mode": bool(json),
        "response_format": (OPENAI_JSON_MODE if bool(json) else "off"),
        "data_ok": bool(data),
        "timings": {"gen_ms": gen_ms},
    }
    if raw:
        resp["raw"] = (raw_text or "")[:2000]
    if diag:
        try:

            def _shape(x):
                try:
                    if hasattr(x, "model_dump"):
                        x = x.model_dump()
                    return json.dumps(x, ensure_ascii=False)[:800]
                except Exception:
                    s = str(x)
                    return s[:800] + ("…" if len(s) > 800 else "")

            resp["shape"] = {
                "content_type": type(getattr(msg, "content", None)).__name__,
                "message": _shape(msg),
            }
        except Exception:
            pass
    return resp


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
