#!/usr/bin/env python3
import csv, re, time, argparse, requests
from bs4 import BeautifulSoup
from tqdm import tqdm

HEAD = {"User-Agent":"Mozilla/5.0 (X11; Linux) Chrome/126 Safari/537.36"}
TG_RE = re.compile(r'(https?://t\.me/[A-Za-z0-9_]+|@[A-Za-z0-9_]{4,})', re.I)
WA_RE = re.compile(r'https?://(?:wa\.me|api\.whatsapp\.com)/\d+', re.I)
S = requests.Session(); S.headers.update(HEAD); S.timeout=10

def parse(html:str):
    soup=BeautifulSoup(html,'html.parser'); t=soup.get_text(' ',strip=True)
    tg=TG_RE.search(t); wa=WA_RE.search(t)
    for a in soup.find_all('a',href=True):
        if not tg and TG_RE.search(a['href']): tg=TG_RE.search(a['href'])
        if not wa and WA_RE.search(a['href']): wa=WA_RE.search(a['href'])
    return (tg.group(0) if tg else '', wa.group(0) if wa else '')

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--input',default='raw_sellers.csv')
    ap.add_argument('--output',default='socials.csv')
    ap.add_argument('--delay',type=float,default=.15)
    a=ap.parse_args()
    rows=list(csv.DictReader(open(a.input,newline='',encoding='utf-8')))
    fn=list(rows[0].keys())+['telegram','whatsapp']
    for r in tqdm(rows):
        try:
            h=S.get(f"https://www.wildberries.ru/seller/{r['supplier_id']}").text
            r['telegram'],r['whatsapp']=parse(h)
        except: r['telegram']=r['whatsapp']=''
        time.sleep(a.delay)
    csv.DictWriter(open(a.output,'w',newline='',encoding='utf-8'),fieldnames=fn)\
        .writeheader(); csv.writer(open(a.output,'a',newline='',encoding='utf-8'))\
        .writerows([r.values() for r in rows])
    print("Done:",a.output)
if __name__=='__main__': main()

