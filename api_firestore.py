# language: Python, file: api_firestore.py
import os, sys, re, codecs, time
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.responses import RedirectResponse
import firebase_admin
from firebase_admin import credentials, firestore

KEY = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', 'serviceAccountKey.json')
if not os.path.exists(KEY):
    print(f'[!] chave nao encontrada: {KEY}', file=sys.stderr); sys.exit(1)

firebase_admin.initialize_app(credentials.Certificate(KEY))
db = firestore.client()
app = FastAPI(title='StreamAPI', version='1.0')
import os as _os
if _os.path.isdir('static'):
    app.mount('/static', StaticFiles(directory='static'), name='static')

def _doc(doc):
    d = doc.to_dict() or {}
    d['id'] = doc.id
    for k, v in list(d.items()):
        if hasattr(v, 'isoformat'): d[k] = v.isoformat()
    return d

@app.get('/')
def home():
    return FileResponse('static/index.html')

@app.get('/health')
def health():
    try:
        n = len(list(db.collection('animes').limit(1).stream()))
        return {'ok': True, 'firestore': 'reachable', 'animes_visible': n > 0}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

@app.get('/animes')
def listar(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
           genero: Optional[str] = None, source: Optional[str] = None):
    q = db.collection('animes')
    if genero: q = q.where('generos', 'array_contains', genero)
    if source: q = q.where('source', '==', source)
    q = q.order_by('scraped_at', direction=firestore.Query.DESCENDING).limit(limit).offset(offset)
    docs = [_doc(d) for d in q.stream()]
    for d in docs:
        d.pop('episodes', None); d.pop('episodios', None)
    return {'count': len(docs), 'limit': limit, 'offset': offset, 'animes': docs}

@app.get('/animes/{anime_id}')
def detalhe(anime_id: str):
    doc = db.collection('animes').document(anime_id).get()
    if not doc.exists: raise HTTPException(404, 'anime nao encontrado')
    return _doc(doc)

@app.get('/animes/{anime_id}/episodes')
def eps(anime_id: str):
    ref = db.collection('animes').document(anime_id)
    if not ref.get().exists: raise HTTPException(404, 'anime nao encontrado')
    eps = [_doc(d) for d in ref.collection('episodes').stream()]
    total = len(eps)
    eps = [e for e in eps if e.get('status') != 'dead']
    eps.sort(key=lambda e: (e.get('numero') is None, e.get('numero') or 0))
    return {'anime_id': anime_id, 'count': len(eps), 'total_including_dead': total, 'episodes': eps}

@app.get('/episodes/{anime_id}/{ep_id}')
def ep(anime_id: str, ep_id: str):
    doc = db.collection('animes').document(anime_id).collection('episodes').document(ep_id).get()
    if not doc.exists: raise HTTPException(404, 'episodio nao encontrado')
    return _doc(doc)

_CACHE = {}
async def _resolve_blogger(ep_url, embed_url=None):
    """Abre o TOKEN diretamente no Blogger com referer correto."""
    from playwright.async_api import async_playwright
    if not embed_url:
        return None, 0

    raw = []
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
            viewport={'width':1920,'height':1080}, locale='pt-BR',
            extra_http_headers={'Referer': 'https://www.blogger.com/'})

        async def on_resp(r):
            u = r.url
            if 'googlevideo.com/videoplayback' in u:
                raw.append(('d', u)); return
            if 'batchexecute' in u:
                try: raw.append(('b', await r.text()))
                except Exception: pass

        ctx.on('response', on_resp)
        pg = await ctx.new_page()
        await pg.goto(embed_url, wait_until='domcontentloaded', timeout=30000)
        await pg.wait_for_timeout(5000)

        for sel in ['video', '.play-button', '[class*="play"]', 'button']:
            try:
                el = await pg.query_selector(sel)
                if el:
                    await el.click(timeout=1500)
                    await pg.wait_for_timeout(2500)
            except Exception: pass

        for _ in range(30):
            if any(k == 'd' for k, _ in raw): break
            await pg.wait_for_timeout(1000)
        await b.close()

    found = {}
    for kind, data in raw:
        if kind == 'd':
            m = re.search(r'[?&]itag=(\d+)', data)
            if m: found.setdefault(int(m.group(1)), data)
            continue
        dec = data
        for _ in range(3):
            dec = dec.replace('\\\\u003d','=').replace('\\\\u0026','&').replace('\\\\/','/')
            dec = dec.replace('\\u003d','=').replace('\\u0026','&').replace('\\/','/')
        for m in re.finditer(r'https://[^\s"]+googlevideo\.com/videoplayback[^\s"]*', dec):
            u = m.group(0).rstrip('\\').rstrip('"').rstrip(']').rstrip(',')
            mi = re.search(r'[?&]itag=(\d+)', u)
            if mi: found.setdefault(int(mi.group(1)), u)

    if not found:
        return None, 0
    url = found.get(22) or found.get(18) or next(iter(found.values()))
    m = re.search(r'[?&]expire=(\d+)', url)
    return url, int(m.group(1)) if m else int(time.time()) + 3600

@app.get('/resolve/{anime_id}/{ep_id}')
async def resolve(anime_id: str, ep_id: str):
    doc = db.collection('animes').document(anime_id).collection('episodes').document(ep_id).get()
    if not doc.exists: raise HTTPException(404, 'episodio nao encontrado')
    e = _doc(doc)
    embed = e.get('embed_url')
    if not embed: raise HTTPException(422, 'sem embed_url')
    token = e.get('embed_id') or embed
    now = time.time()
    if token in _CACHE:
        u, exp = _CACHE[token]
        if exp - now > 300:
            return {'url': u, 'cached': True, 'expire': exp}
    if 'blogger.com' in embed:
        url, exp = await _resolve_blogger(e.get('url'), embed)
        if not url:
            db.collection('animes').document(anime_id).collection('episodes').document(ep_id)\
              .update({'status': 'dead', 'checked_at': firestore.SERVER_TIMESTAMP})
            raise HTTPException(502, 'video morto no blogger')
        db.collection('animes').document(anime_id).collection('episodes').document(ep_id)\
          .update({'status': 'alive', 'checked_at': firestore.SERVER_TIMESTAMP})
        _CACHE[token] = (url, exp)
        return {'url': url, 'cached': False, 'expire': exp}
    return {'url': embed, 'cached': False, 'expire': 0, 'type': e.get('embed_type')}

@app.get('/stream/{anime_id}/{ep_id}')
async def stream(anime_id: str, ep_id: str):
    r = await resolve(anime_id, ep_id)
    return RedirectResponse(r['url'], status_code=302)

if __name__ == '__main__':
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f'[*] API em http://0.0.0.0:{port}  |  docs: /docs')
    uvicorn.run(app, host='0.0.0.0', port=port, log_level='info')
