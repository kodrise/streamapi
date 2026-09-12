#!/usr/bin/env python3
import asyncio, argparse
import firebase_admin
from firebase_admin import credentials, firestore
from playwright.async_api import async_playwright

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
def log(*a): print(*a, flush=True)

async def validar(ctx, url, timeout=20):
    if not url or 'blogger.com' not in url: return None
    pg = await ctx.new_page()
    ok = {'v': False}
    async def on_resp(r):
        if 'googlevideo.com/videoplayback' in r.url: ok['v'] = True
        if 'batchexecute' in r.url:
            try:
                if 'googlevideo' in await r.text(): ok['v'] = True
            except: pass
    pg.on('response', on_resp)
    try:
        await pg.goto(url, wait_until='domcontentloaded', timeout=timeout*1000)
        for _ in range(timeout):
            if ok['v']: break
            await pg.wait_for_timeout(1000)
    except: pass
    finally: await pg.close()
    return ok['v']

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--firebase-key', required=True)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=5)
    ap.add_argument('--only-unknown', action='store_true')
    a = ap.parse_args()

    firebase_admin.initialize_app(credentials.Certificate(a.firebase_key))
    db = firestore.client()
    log('[*] coletando eps...')
    eps = []
    for an in db.collection('animes').stream():
        for ep in db.collection('animes').document(an.id).collection('episodes').stream():
            d = ep.to_dict() or {}
            if not d.get('embed_url'): continue
            if a.only_unknown and d.get('status') in ('alive','dead'): continue
            eps.append((an.id, ep.id, d['embed_url'], d.get('numero') or 0))
    if a.limit: eps = eps[:a.limit]
    log(f'[*] {len(eps)} eps')

    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(user_agent=UA, viewport={'width':1280,'height':720}, locale='pt-BR')
        sem = asyncio.Semaphore(a.workers)
        c = {'ok':0,'dead':0,'err':0}
        async def w(aid, eid, url, num):
            async with sem:
                try:
                    r = await validar(ctx, url)
                    if r is True:
                        c['ok']+=1; st='alive'; log(f'  [alive] {aid} ep{num} ({eid})')
                    elif r is False:
                        c['dead']+=1; st='dead'; log(f'  [DEAD]  {aid} ep{num} ({eid})')
                    else: return
                    db.collection('animes').document(aid).collection('episodes').document(eid)\
                      .update({'status': st, 'checked_at': firestore.SERVER_TIMESTAMP})
                except Exception as e:
                    c['err']+=1; log(f'  [erro] {aid} ep{num}: {e}')
        tasks = [w(*e) for e in eps]
        for i in range(0, len(tasks), a.workers*3):
            await asyncio.gather(*tasks[i:i+a.workers*3])
            log(f'  {min(i+a.workers*3,len(tasks))}/{len(tasks)} | ok:{c["ok"]} dead:{c["dead"]} err:{c["err"]}')
        await b.close()
    log(f'[+] alive {c["ok"]}, dead {c["dead"]}, erro {c["err"]}')

asyncio.run(main())
