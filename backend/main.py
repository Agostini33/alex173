from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, openai, hashlib, jwt, datetime, json, requests, logging, secrets
from bs4 import BeautifulSoup

# ============================
# 🔐 Загрузка секретов из env
# ============================

ENV = os.getenv("ENV", "DEV").upper()
PROD = ENV == "PRODUCTION"

# ✅ OpenAI API Key (обязательно: без него переписывание не работает)
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    raise ValueError("❌ OPENAI_API_KEY не установлен. Укажите его в Railway/GitHub Secrets.")
client = openai.OpenAI(api_key=OPENAI_KEY)

# ✅ Robokassa Pass2 (используется для подписи callback'ов)
PASS2 = os.getenv("ROBOKASSA_PASS2")
if not PASS2:
    logging.warning("ROBOKASSA_PASS2 не установлен: оплата отключена")
    if not PROD:
        PASS2 = "dev-pass2"

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

PROMPT = """
Ты опытный SEO-копирайтер маркетплейса Wildberries.
Тебе дают исходный текст карточки товара. Сгенерируй:
1) Новый заголовок ≤100 символов.
2) 6 буллитов ≤120 символов каждый.
3) 20 ключевых фраз CSV.
Тон разговорный, без канцелярита, язык Русский.
Не упоминай «Wildberries», «скидка», %. Верни JSON:
{
 "title":"…",
 "bullets":["…","…","…","…","…","…"],
 "keywords":["k1","k2",…,"k20"]
}
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
@app.options("/rewrite", include_in_schema=False)   # pre-flight
async def _pre(): return {}

# ── helpers ───────────────────────────────────
def issue(email:str, quota:int):
    return jwt.encode(
        {"sub":email, "quota":quota,
         "exp":datetime.datetime.utcnow()+datetime.timedelta(days=30)},
        SECRET, algorithm="HS256")

def verify(tok:str):
    try: return jwt.decode(tok, SECRET, algorithms=["HS256"])
    except: return None

def send_email(to:str, login:str, password:str):
    logging.info(f"Email to {to}: login={login} password={password}")

def create_account(email:str, quota:int, inv:str):
    login = secrets.token_hex(4)
    password = secrets.token_hex(4)
    ACCOUNTS[email] = {"login": login, "password": password, "quota": quota}
    LOGIN_INDEX[login] = email
    token = issue(email, quota)
    TOKENS[inv] = token
    send_email(email, login, password)
    return token

def wb_text(url:str) -> str:
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
    supplierId:int
    prompt:str

@app.post("/rewrite")
async def rewrite(r:Req, request:Request):
    info = verify(request.headers.get("Authorization","").replace("Bearer ",""))
    if not info: info = {"sub":"anon","quota":3}   # 3 free
    if info["quota"]<=0:
        return {"error":"NO_CREDITS"}
    prompt = r.prompt.strip()
    if prompt.startswith("http") and "wildberries.ru" in prompt:
        prompt = wb_text(prompt)
    try:
        comp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":PROMPT},
                      {"role":"user","content":prompt}]
        )
    except Exception as e:
        return {"error": str(e)}
    info["quota"] -= 1
    if info["sub"] in ACCOUNTS:
        ACCOUNTS[info["sub"]]["quota"] = info["quota"]
    return {"token": jwt.encode(info, SECRET, "HS256"),
            **json.loads(comp.choices[0].message.content)}

# Robokassa ResultURL
@app.post("/payhook")
async def payhook(req:Request):
    f = await req.form()
    crc = hashlib.md5(f"{f['InvId']}:{f['OutSum']}:{PASS2}:shp={f['Shp_plan']}".encode()).hexdigest().upper()
    if crc != f['SignatureValue'].upper(): return "bad sign"
    quota = 15 if f['Shp_plan']=="15" else 60
    email = f.get("Email", "user@wb6")
    create_account(email, quota, f['InvId'])
    return "OK"

class LoginReq(BaseModel):
    login:str
    password:str

@app.post("/login")
async def login(r:LoginReq):
    email = LOGIN_INDEX.get(r.login)
    if not email:
        return {"error":"AUTH_FAILED"}
    acc = ACCOUNTS.get(email)
    if acc and acc["password"] == r.password:
        return {"token": issue(email, acc["quota"])}
    return {"error":"AUTH_FAILED"}

@app.get("/paytoken")
async def paytoken(inv:int):
    tok = TOKENS.pop(str(inv), None)
    if tok:
        return {"token": tok}
    return {"error":"NOT_READY"}

