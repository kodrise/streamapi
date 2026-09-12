#!/usr/bin/env python3
# language: Python, file: scrape.py
# scraper + Firestore em lotes, num arquivo so
# uso: python scrape.py "https://goyabu.io/lista-de-animes" -o catalogo.json --firebase-key serviceAccountKey.json

import asyncio, json, re, sys, argparse, os
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# ============ FIREBASE INLINE ============
import firebase_admin
from firebase_admin import credentials, firestore

_fb = {'db': None, 'on': False}

def fb_init(key_path):
    if _fb['db'] is None:
        firebase_admin.initialize_app(credentials.Certificate(key_path))
        _fb['db'] = firestore.client()
    _fb['on'] = True
    return _fb['db']

def fb_push_anime(anime):
    if not _fb['on']: return False, 'off'
    aid = anime.get('id') or anime.get('slug')
    if not aid: return False, 'sem id'
    eps = anime.get('episodios', anime.get('episodes', []))
    ref = _fb['db'].collection('animes').document(aid)
    doc = {k: v for k, v in anime.items() if k not in ('episodios', 'episodes')}
    doc['episodes_count'] = len(eps)
    doc['updated_at'] = firestore.SERVER_TIMESTAMP
    ref.set(doc, merge=True)
    for ep in eps:
        eid = ep.get('id') or f"ep-{ep.get('numero') or 0:03d}"
        d = dict(ep); d['updated_at'] = firestore.SERVER_TIMESTAMP
        ref.collection('episodes').document(eid).set(d, merge=True)
    return True, aid

def fb_push_ep(anime_id, ep):
    if not _fb['on']: return False, 'off'
    if not anime_id: return False, 'sem anime_id'
    eid = ep.get('id') or f"ep-{ep.get('numero') or 0:03d}"
    d = dict(ep); d['updated_at'] = firestore.SERVER_TIMESTAMP
    _fb['db'].collection('animes').document(anime_id).collection('episodes').document(eid).set(d, merge=True)
    return True, eid

def fb_push_meta(meta):
    if not _fb['on']: return
    try:
        _fb['db'].collection('_meta').document('last_scrape').set(
            {**meta, 'updated_at': firestore.SERVER_TIMESTAMP}, merge=True)
    except Exception:
        pass

def fb_anime_pronto(aid, min_eps=1):
    """True se o anime existe no Firestore com eps resolvidos."""
    if not _fb['on'] or not aid: return False
    try:
        ref = _fb['db'].collection('animes').document(aid)
        doc = ref.get()
        if not doc.exists: return False
        d = doc.to_dict() or {}
        if (d.get('episodes_count') or 0) < min_eps: return False
        amostra = list(ref.collection('episodes').limit(3).stream())
        if not amostra: return False
        return any((e.to_dict() or {}).get('embed_url') for e in amostra)
    except Exception:
        return False

def fb_ep_pronto(aid, eid):
    if not _fb['on'] or not aid or not eid: return False
    try:
        doc = _fb['db'].collection('animes').document(aid).collection('episodes').document(eid).get()
        if not doc.exists: return False
        return bool((doc.to_dict() or {}).get('embed_url'))
    except Exception:
        return False

# ============ HELPERS ============
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

def log(*a): print(*a, flush=True)
def iso_now(): return datetime.now(timezone.utc).isoformat()

def embed_token_id(u):
    if not u: return None
    m = re.search(r'token=([A-Za-z0-9_\-]+)', u)
    return m.group(1) if m else None

# ============ ESTAGIO 1: LISTAR ============
async def listar(ctx, base_url, max_pag=200, stop_after=0, pages_limit=0):
    base_clean = base_url.split('?')[0].rstrip('/')
    letras = ['0-9'] + [chr(c) for c in range(ord('a'), ord('z')+1)]
    vistos, itens = set(), []
    pg = await ctx.new_page()
    parar = False
    for letra in letras:
        if parar: break
        page = 1
        while page <= max_pag:
            if stop_after and len(itens) >= stop_after:
                parar = True; break
            url = f'{base_clean}?l={letra}' if page == 1 else f'{base_clean}/page/{page}?l={letra}'
            log(f'[1] letra={letra} pag={page}')
            try:
                await pg.goto(url, wait_until='networkidle', timeout=45000)
                await pg.wait_for_timeout(2200)
                for _ in range(5):
                    await pg.mouse.wheel(0, 4000); await pg.wait_for_timeout(500)
            except Exception as e:
                log(f'[1] erro: {e}'); break
            soup = BeautifulSoup(await pg.content(), 'lxml')
            novos = 0
            for a in soup.select('a[href*="/anime/"]'):
                href = a['href']
                if not re.search(r'/anime/[^/]+/?$', href): continue
                link = urljoin(base_url, href)
                if link in vistos: continue
                vistos.add(link)
                capa = None
                img = a.find('img')
                if img:
                    capa = img.get('src') or img.get('data-src')
                    if capa: capa = urljoin(base_url, capa)
                itens.append({'url': link, 'capa': capa})
                novos += 1
                if stop_after and len(itens) >= stop_after: break
            log(f'[1] letra={letra} pag={page}: +{novos} (total {len(itens)})')
            if novos == 0: break
            if pages_limit and page >= pages_limit: break
            page += 1
    await pg.close()
    return itens[:stop_after] if stop_after else itens

# ============ ESTAGIO 2: DETALHES ============
def extrair_detalhes(html, url, capa_listagem=None):
    soup = BeautifulSoup(html, 'lxml')
    out = {'url': url, 'titulo': None, 'titulo_original': None, 'capa': capa_listagem,
           'sinopse': None, 'ano': None, 'nota': None, 'status': None,
           'generos': [], 'episodios': []}

    h1 = soup.find('h1')
    if h1: out['titulo'] = h1.get_text(' ', strip=True)
    if not out['titulo'] and soup.title:
        out['titulo'] = re.split(r'\s*[-|–]\s*', soup.title.string or '', 1)[0].strip()

    for sel in ['.original-title', '.title-english', '.subtitle', '.ingles']:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            out['titulo_original'] = el.get_text(' ', strip=True); break

    for sel in ['.poster img', '.thumb img', 'img.cover', '.cover img']:
        el = soup.select_one(sel)
        if el:
            src = el.get('src') or el.get('data-src')
            if src: out['capa'] = urljoin(url, src); break
    if not out['capa']:
        meta = soup.find('meta', property='og:image')
        if meta: out['capa'] = meta.get('content')

    for sel in ['.description', '.sinopse', '.wp-content p', '.info .desc', '.desc']:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(' ', strip=True)
            if len(txt) > 40: out['sinopse'] = txt; break
    if not out['sinopse']:
        meta = soup.find('meta', property='og:description') or soup.find('meta', attrs={'name':'description'})
        if meta: out['sinopse'] = meta.get('content','').strip()

    for sel in ['[itemprop="datePublished"]', '.year', '.ano', '.release-year', '.data-ano']:
        el = soup.select_one(sel)
        if el:
            m = re.search(r'\b(19[5-9]\d|20[0-3]\d)\b', el.get_text())
            if m: out['ano'] = int(m.group(0)); break
    if not out['ano']:
        for m in re.finditer(r'\b(19[5-9]\d|20[0-3]\d)\b', soup.get_text()):
            out['ano'] = int(m.group(0)); break

    for sel in ['.rating-poster', '.rating', '.nota', '[itemprop="ratingValue"]', '.score']:
        el = soup.select_one(sel)
        if el:
            m = re.search(r'\d+(?:\.\d+)?', el.get_text())
            if m: out['nota'] = float(m.group(0)); break

    for sel in ['.status', '.estado', '.situacao']:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            out['status'] = el.get_text(strip=True); break

    for a in soup.select('a[href*="/genero/"], a[href*="/generos/"], .genres a, .sgeneros a, .generos a'):
        g = a.get_text(strip=True)
        if g and g not in out['generos'] and len(g) < 40: out['generos'].append(g)

    vistos = set()
    for a in soup.find_all('a', href=True):
        href = a['href']
        m_id = re.search(r'/(\d{3,8})/?$', href)
        m_ep = re.search(r'/(?:episodio|ep|epi)[-/]?(\d+)', href, re.I)
        if not (m_id or m_ep): continue
        if re.search(r'/(anime|genero|lista|perfil|login|calendario|random|populares|lancamentos|wp-|page)/', href): continue
        ep_url = urljoin(url, href)
        if ep_url in vistos or ep_url == url: continue
        vistos.add(ep_url)
        raw = a.get_text(' ', strip=True)
        raw = re.sub(r'\s*h[áa]\s+\d+\s*(?:d|dia|dias|h|hora|horas|min|m[êe]s|meses|ano|anos|sem|semana|semanas).*$', '', raw, flags=re.I).strip()
        raw = re.sub(r'\s*[A-Z][a-z]{2}\.?\s*\d{1,2},?\s*\d{4}.*$', '', raw).strip()
        m = re.search(r'(\d+)', raw)
        numero = int(m.group(1)) if m else (int(m_id.group(1)) if m_id else (int(m_ep.group(1)) if m_ep else None))
        out['episodios'].append({'numero': numero,
                                 'titulo': raw or f'Ep {numero or len(out["episodios"])+1}',
                                 'url': ep_url})
    out['episodios'].sort(key=lambda e: (e['numero'] is None, e['numero'] or 0))
    return out

# ============ ESTAGIO 3: EMBED ============
async def capturar_embed(ctx, ep_url, timeout=25):
    pg = await ctx.new_page()
    capturas = []
    async def on_resp(r):
        u = r.url
        if any(x in u for x in ['blogger.com/video.g', 'youtube.com/embed', 'youtu.be/',
                                 'playerflix', 'myembed', 'dood', 'streamtape', 'mixdrop',
                                 'admin-ajax.php', 'dooplayer']):
            try:
                ct = r.headers.get('content-type','')
                if 'json' in ct or 'admin-ajax' in u or 'dooplayer' in u:
                    capturas.append({'kind':'json','url':u,'body': (await r.text())[:4000]})
                else:
                    capturas.append({'kind':'url','url':u})
            except Exception:
                capturas.append({'kind':'url','url':u})
    pg.on('response', on_resp)
    embed, tipo = None, None
    try:
        await pg.goto(ep_url, wait_until='networkidle', timeout=timeout*1000)
        await pg.wait_for_timeout(4000)
        for sel in ['video', '.play-button', '[class*="play"]', 'button', '.server-item', 'a.server']:
            try:
                el = await pg.query_selector(sel)
                if el:
                    await el.click(timeout=1500); await pg.wait_for_timeout(2000)
            except Exception: pass
        for _ in range(8):
            if capturas: break
            await pg.wait_for_timeout(1000)
        iframes = await pg.eval_on_selector_all('iframe', 'els => els.map(e => e.src)')
        for src in iframes:
            if src: capturas.append({'kind':'iframe','url':src})
    except Exception: pass
    finally:
        await pg.close()
    for c in capturas:
        u = c['url']
        if 'blogger.com/video.g' in u: embed, tipo = u, 'blogger'; break
        if 'youtube.com/embed' in u or 'youtu.be/' in u: embed, tipo = u, 'youtube'; break
        if c['kind'] == 'json':
            for pat in [r'\{.*\}', r'\[.*\]']:
                m = re.search(pat, c['body'], re.S)
                if not m: continue
                try:
                    j = json.loads(m.group(0))
                    if isinstance(j, dict) and j.get('embed_url'):
                        embed, tipo = j['embed_url'], j.get('type') or 'iframe'; break
                except Exception: pass
            if embed: break
        if c['kind'] == 'iframe' and not embed:
            embed, tipo = u, 'iframe'
    return embed, tipo


async def validar_token_blogger(ctx, embed_url, timeout=20):
    if not embed_url or 'blogger.com' not in embed_url:
        return None
    pg = await ctx.new_page()
    achou = {'ok': False}
    async def on_resp(r):
        if 'googlevideo.com/videoplayback' in r.url:
            achou['ok'] = True
        if 'batchexecute' in r.url:
            try:
                if 'googlevideo' in await r.text():
                    achou['ok'] = True
            except Exception:
                pass
    pg.on('response', on_resp)
    try:
        await pg.goto(embed_url, wait_until='domcontentloaded', timeout=timeout*1000)
        for _ in range(timeout):
            if achou['ok']: break
            await pg.wait_for_timeout(1000)
    except Exception:
        pass
    finally:
        await pg.close()
    return achou['ok']

# ============ PIPELINE: lote a lote ============
async def pipeline(base_url, out_path, workers_detail=3, workers_embed=3,
                   stop_after=0, firebase_key=None, batch=100, skip_existing=True, pages_limit=0):
    if firebase_key:
        fb_init(firebase_key)
        log(f'[fb] ativado: {firebase_key}')

    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(user_agent=UA, viewport={'width':1920,'height':1080}, locale='pt-BR')

        # se for URL de anime especifico, pula o estagio 1
        if re.search(r'/anime/[^/]+/?$', base_url):
            log(f'[1] URL de anime especifico, pulando listagem')
            animes = [{'url': base_url, 'capa': None}]
        else:
            animes = await listar(ctx, base_url, stop_after=stop_after, pages_limit=pages_limit)
        log(f'[=] {len(animes)} animes na fila')

        resultado = [{'url': a['url'], 'capa': a.get('capa')} for a in animes]

        def salvar():
            payload = {
                'meta': {'source': urlparse(base_url).netloc, 'scraped_at': iso_now(),
                         'total_animes': len(resultado), 'version': '1.0'},
                'animes': resultado
            }
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

        sem2 = asyncio.Semaphore(workers_detail)
        sem3 = asyncio.Semaphore(workers_embed)
        stats = {'detalhe_skip': 0, 'detalhe_ok': 0, 'detalhe_erro': 0,
                 'embed_ok': 0, 'embed_skip': 0, 'embed_sem': 0, 'embed_dead': 0}

        async def detalhe(i, item):
            async with sem2:
                slug = urlparse(item['url']).path.strip('/').split('/')[-1]
                if skip_existing and fb_anime_pronto(slug):
                    stats['detalhe_skip'] += 1
                    log(f'[2] {i+1}/{len(animes)} SKIP {slug}')
                    resultado[i]['id'] = slug; resultado[i]['slug'] = slug
                    resultado[i]['_skipped'] = True
                    try:
                        eps_docs = _fb['db'].collection('animes').document(slug).collection('episodes').stream()
                        resultado[i]['episodios'] = [dict(e.to_dict(), id=e.id) for e in eps_docs]
                    except Exception:
                        resultado[i]['episodios'] = []
                    return
                pg = await ctx.new_page()
                try:
                    await pg.goto(item['url'], wait_until='networkidle', timeout=45000)
                    await pg.wait_for_timeout(3000)
                    for _ in range(4):
                        await pg.mouse.wheel(0, 4000); await pg.wait_for_timeout(500)
                    html = await pg.content()
                    info = extrair_detalhes(html, item['url'], capa_listagem=item.get('capa'))
                    info['id'] = slug; info['slug'] = slug
                    info['source'] = urlparse(item['url']).netloc
                    info['scraped_at'] = iso_now()
                    resultado[i].update(info)
                    stats['detalhe_ok'] += 1
                    log(f'[2] {i+1}/{len(animes)} {slug} | {str(info.get("titulo"))[:48]} ({len(info.get("episodios",[]))} eps)')
                    if _fb['on']:
                        try:
                            await asyncio.to_thread(fb_push_anime, resultado[i])
                        except Exception as e:
                            log(f'      [fb] erro anime: {e}')
                except Exception as e:
                    resultado[i]['erro_detalhe'] = str(e)
                    stats['detalhe_erro'] += 1
                    log(f'[2] {i+1}/{len(animes)} ERRO: {e}')
                finally:
                    await pg.close()

        async def embed_ep(ai, ei, ep):
            async with sem3:
                aid = resultado[ai].get('id') or resultado[ai].get('slug')
                eid = f"ep-{(ep.get('numero') or (ei+1)):03d}"
                if skip_existing and fb_ep_pronto(aid, eid):
                    stats['embed_skip'] += 1
                    ep['id'] = eid
                    return
                embed, tipo = await capturar_embed(ctx, ep['url'])
                d = resultado[ai]['episodios'][ei]
                d['embed_url'] = embed; d['embed_type'] = tipo
                d['embed_id'] = embed_token_id(embed)
                d['id'] = eid
                d['scraped_at'] = iso_now()
                if embed:
                    vivo = await validar_token_blogger(ctx, embed)
                    if vivo is True:
                        d['status'] = 'alive'
                        stats['embed_ok'] += 1
                        log(f'[3] ok {resultado[ai].get("id","?")} ep{ep.get("numero") or ei+1} -> {str(embed)[:50]}')
                    elif vivo is False:
                        d['status'] = 'dead'
                        stats['embed_dead'] += 1
                        log(f'[3] DEAD {resultado[ai].get("id","?")} ep{ep.get("numero") or ei+1}')
                    else:
                        d['status'] = 'unknown'
                        stats['embed_ok'] += 1
                        log(f'[3] ok {resultado[ai].get("id","?")} ep{ep.get("numero") or ei+1} ({tipo})')
                else:
                    d['status'] = 'no_embed'
                    stats['embed_sem'] += 1
                    log(f'[3] SEM {resultado[ai].get("id","?")} ep{ep.get("numero") or ei+1}')
                if _fb['on']:
                    try:
                        await asyncio.to_thread(fb_push_ep, aid, d)
                    except Exception as e:
                        log(f'      [fb] erro ep: {e}')

        async def processar_lote(idx_lote, lote):
            log(f'\n[===] LOTE {idx_lote} ({len(lote)} animes) ===')
            await asyncio.gather(*(detalhe(i, item) for i, item in lote))
            tarefas = []
            for i, _ in lote:
                for ei, ep in enumerate(resultado[i].get('episodios', [])):
                    tarefas.append(embed_ep(i, ei, ep))
            tam = workers_embed * 2
            for k in range(0, len(tarefas), tam):
                await asyncio.gather(*tarefas[k:k+tam])
            salvar()
            log(f'[===] LOTE {idx_lote} pronto | detalhes: {stats["detalhe_ok"]} ok / {stats["detalhe_skip"]} skip | embeds: {stats["embed_ok"]} ok / {stats["embed_skip"]} skip / {stats["embed_sem"]} sem')

        pares = list(enumerate(resultado))
        tamanho = batch if batch and batch > 0 else len(pares)
        total_lotes = (len(pares) + tamanho - 1) // tamanho

        for idx in range(total_lotes):
            lote = pares[idx*tamanho : (idx+1)*tamanho]
            await processar_lote(idx+1, lote)

        salvar()
        fb_push_meta({'source': urlparse(base_url).netloc, 'total_animes': len(resultado),
                      'stats': stats, 'finished_at': iso_now()})
        await b.close()

    log(f'\n[=] PRONTO -> {out_path}')
    log(f'[=] stats finais: {stats}')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('url')
    ap.add_argument('-o', '--output', default='catalogo.json')
    ap.add_argument('--stop-after', type=int, default=0)
    ap.add_argument('--workers-detail', type=int, default=3)
    ap.add_argument('--workers-embed', type=int, default=3)
    ap.add_argument('--firebase-key', default=None)
    ap.add_argument('--batch', type=int, default=100, help='tamanho do lote (padrao 100)')
    ap.add_argument('--no-skip', action='store_true', help='desliga skip de itens ja existentes')
    ap.add_argument('--pages-limit', type=int, default=0, help='limita paginas por letra (0 = todas)')
    ap.add_argument('--mode', choices=['auto','full','home'], default='full', help='auto/full/home')
    args = ap.parse_args()
    if args.mode == 'home':
        asyncio.run(pipeline_home(args.home_url or args.url, args.output,
                                  workers=args.workers_embed,
                                  firebase_key=args.firebase_key,
                                  skip_existing=not args.no_skip))
    else:
        asyncio.run(pipeline(args.url, args.output,
                             workers_detail=args.workers_detail,
                             workers_embed=args.workers_embed,
                             stop_after=args.stop_after,
                             firebase_key=args.firebase_key,
                             batch=args.batch,
                             skip_existing=not args.no_skip,
                             pages_limit=args.pages_limit))

if __name__ == '__main__':
    main()
