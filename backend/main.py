from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, openai, hashlib, jwt, datetime, json, requests
from bs4 import BeautifulSoup

openai.api_key = os.getenv("OPENAI_API_KEY")
PASS2  = os.getenv("ROBOKASSA_PASS2", "pass2")
SECRET = os.getenv("JWT_SECRET",  "wb6secret")   # сгенерируйте:  python - <<EOF
                                                 # import secrets,base64,os,sys,hashlib,json

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
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://wb6.ru",
        "http://wb6.ru",
        "https://www.wb6.ru",
        "http://www.wb6.ru",
        "https://wb6.vercel.app",
    ],
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
    comp = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[{"role":"system","content":PROMPT},
                  {"role":"user","content":prompt}]
    )
    info["quota"] -= 1
    return {"token": jwt.encode(info, SECRET, "HS256"),
            **json.loads(comp.choices[0].message.content)}

# Robokassa ResultURL
@app.post("/payhook")
async def payhook(req:Request):
    f = await req.form()
    crc = hashlib.md5(f"{f['InvId']}:{f['OutSum']}:{PASS2}:shp={f['Shp_plan']}".encode()).hexdigest().upper()
    if crc != f['SignatureValue'].upper(): return "bad sign"
    quota = 15 if f['Shp_plan']=="15" else 60
    token = issue(f.get("Email","user@wb6"), quota)
    # TODO: отправить письмо с token
    return "OK"

