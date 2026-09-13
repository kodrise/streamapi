#!/usr/bin/env python3
# language: Python, file: orion.py
# scraper do animesorion.cc — reusa helpers do scrape.py
import asyncio, json, re, sys, argparse
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from scrape import (
    _json_default,
    fb_init, fb_push_anime, fb_push_ep, fb_push_meta,
    fb_anime_pronto, fb_ep_pronto, fb_mark_full_scan, fb_full_scan_done,
    iso_now, embed_token_id, capturar_embed, validar_token_blogger, UA, log,
)

BASE = 'https://animesorion.cc'

async def listar_animes(ctx, listagem, max_pag=50, pages_limit=0):
    """Varre /animes/ (paginado por /page/N/)."""
    base_clean = listagem.rstrip('/')
    vistos, itens = set(), []
    pg = await ctx.new_page()
    page = 1
    while page <= max_pag:
        url = base_clean + '/' if page == 1 else f'{base_clean}/page/{page}/'
        log(f'[1] pag {page}: {url}')
        try:
            await pg.goto(url, wait_until='networkidle', timeout=45000)
            await pg.wait_for_timeout(2500)
            for _ in range(5):
                await pg.mouse.wheel(0, 4000); await pg.wait_for_timeout(500)
        except Exception as e:
            log(f'[1] erro: {e}'); break
        soup = BeautifulSoup(await pg.content(), 'lxml')
        novos = 0
        for a in soup.find_all('a', href=True):
            h = a['href']
            if not re.search(r'/animes/[^/]+/?$', h): continue
            link = urljoin(BASE, h)
            if link in vistos: continue
            vistos.add(link)
            capa = None
            img = a.find('img')
            if img:
                capa = img.get('src') or img.get('data-src')
                if capa: capa = urljoin(BASE, capa)
            itens.append({'url': link, 'capa': capa})
            novos += 1
        log(f'[1] pag {page}: +{novos} (total {len(itens)})')
        if novos == 0: break
        if pages_limit and page >= pages_limit: break
        page += 1
    await pg.close()
    return itens

def extrair_anime(html, url, capa_listagem=None):
    soup = BeautifulSoup(html, 'lxml')
    out = {'url': url, 'titulo': None, 'capa': capa_listagem, 'sinopse': None,
           'ano': None, 'nota': None, 'generos': [], 'episodios': []}

    h1 = soup.find('h1')
    if h1: out['titulo'] = h1.get_text(' ', strip=True)
    if not out['titulo'] and soup.title:
        out['titulo'] = re.split(r'\s*[-|–]\s*', soup.title.string or '', 1)[0].strip()

    if not out['capa']:
        for sel in ['.sertop img', '.poster img', 'img.cover']:
            el = soup.select_one(sel)
            if el:
                src = el.get('src') or el.get('data-src')
                if src: out['capa'] = urljoin(url, src); break
    if not out['capa']:
        meta = soup.find('meta', property='og:image')
        if meta: out['capa'] = meta.get('content')

    for sel in ['.wp-content p', '.description', '.sinopse']:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(' ', strip=True)
            if len(txt) > 40: out['sinopse'] = txt; break

    for a in soup.select('a[href*="/genero/"], a[href*="/generos/"]'):
        g = a.get_text(strip=True)
        if g and g not in out['generos'] and len(g) < 40: out['generos'].append(g)

    vistos = set()
    for a in soup.find_all('a', href=True):
        h = a['href']
        if '/episodios/' not in h: continue
        m = re.search(r'-(\d+)x(\d+)/?$', h)
        if not m: continue
        ep_url = urljoin(url, h)
        if ep_url in vistos or ep_url == url: continue
        vistos.add(ep_url)
        num = int(m.group(2))
        out['episodios'].append({'numero': num, 'titulo': f'Ep {num}', 'url': ep_url})
    out['episodios'].sort(key=lambda e: (e['numero'] is None, e['numero'] or 0))
    return out

async def pipeline(base_url, out_path, firebase_key=None, skip_existing=True,
                   batch=100, pages_limit=0, workers_detail=3, workers_embed=3):
    if firebase_key:
        fb_init(firebase_key)
        log(f'[fb] ativado: {firebase_key}')

    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(user_agent=UA, viewport={'width':1920,'height':1080}, locale='pt-BR')

        animes = await listar_animes(ctx, base_url, pages_limit=pages_limit)
        log(f'[=] {len(animes)} animes na fila')
        resultado = [{'url': a['url'], 'capa': a.get('capa')} for a in animes]

        sem2 = asyncio.Semaphore(workers_detail)
        sem3 = asyncio.Semaphore(workers_embed)
        stats = {'d_ok':0,'d_skip':0,'e_ok':0,'e_skip':0,'e_dead':0,'e_sem':0}

        def salvar():
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump({'meta':{'source':'animesorion.cc','scraped_at':iso_now()},'animes':resultado},
                          f, ensure_ascii=False, indent=2, default=_json_default)

        async def detalhe(i, item):
            async with sem2:
                slug = urlparse(item['url']).path.strip('/').split('/')[-1]
                if skip_existing and fb_anime_pronto(slug):
                    stats['d_skip'] += 1
                    log(f'[2] {i+1}/{len(animes)} SKIP {slug}')
                    resultado[i]['id'] = slug; resultado[i]['slug'] = slug
                    return
                pg = await ctx.new_page()
                try:
                    await pg.goto(item['url'], wait_until='networkidle', timeout=45000)
                    await pg.wait_for_timeout(3000)
                    html = await pg.content()
                    info = extrair_anime(html, item['url'], capa_listagem=item.get('capa'))
                    info['id'] = slug; info['slug'] = slug
                    info['source'] = 'animesorion.cc'
                    info['scraped_at'] = iso_now()
                    resultado[i].update(info)
                    stats['d_ok'] += 1
                    log(f'[2] {i+1}/{len(animes)} {slug} | {str(info.get("titulo"))[:48]} ({len(info.get("episodios",[]))} eps)')
                    if fb_enabled():
                        try: await asyncio.to_thread(fb_push_anime, resultado[i])
                        except Exception: pass
                except Exception as e:
                    log(f'[2] {i+1}/{len(animes)} ERRO: {e}')
                finally:
                    await pg.close()

        def fb_enabled(): return True

        async def embed_ep(ai, ei, ep):
            async with sem3:
                aid = resultado[ai].get('id')
                eid = f"ep-{(ep.get('numero') or (ei+1)):03d}"
                if skip_existing and fb_ep_pronto(aid, eid):
                    stats['e_skip'] += 1; return
                embed, tipo = await capturar_embed(ctx, ep['url'])
                d = resultado[ai]['episodios'][ei]
                d['embed_url'] = embed; d['embed_type'] = tipo
                d['embed_id'] = embed_token_id(embed)
                d['id'] = eid; d['scraped_at'] = iso_now()
                if embed:
                    vivo = await validar_token_blogger(ctx, embed)
                    if vivo is True:
                        d['status']='alive'; stats['e_ok']+=1
                        log(f'[3] ok {aid} ep{ep.get("numero")}')
                    elif vivo is False:
                        d['status']='dead'; stats['e_dead']+=1
                        log(f'[3] DEAD {aid} ep{ep.get("numero")}')
                    else:
                        d['status']='unknown'; stats['e_ok']+=1
                        log(f'[3] ok {aid} ep{ep.get("numero")} ({tipo})')
                else:
                    d['status']='no_embed'; stats['e_sem']+=1
                    log(f'[3] SEM {aid} ep{ep.get("numero")}')
                if fb_enabled():
                    try: await asyncio.to_thread(fb_push_ep, aid, d, 'animesorion.cc')
                    except Exception: pass

        async def processar_lote(idx, lote):
            log(f'\n[===] LOTE {idx} ({len(lote)}) ===')
            await asyncio.gather(*(detalhe(i, it) for i, it in lote))
            tarefas = []
            for i, _ in lote:
                for ei, ep in enumerate(resultado[i].get('episodios', [])):
                    tarefas.append(embed_ep(i, ei, ep))
            for k in range(0, len(tarefas), workers_embed*2):
                await asyncio.gather(*tarefas[k:k+workers_embed*2])
            salvar()
            log(f'[===] LOTE {idx} pronto | {stats}')

        pares = list(enumerate(resultado))
        tam = batch or len(pares)
        for idx in range((len(pares)+tam-1)//tam):
            await processar_lote(idx+1, pares[idx*tam:(idx+1)*tam])

        salvar()
        fb_push_meta({'source':'animesorion.cc','total_animes':len(resultado),'stats':stats,'finished_at':iso_now()})
        await b.close()
    log(f'\n[=] PRONTO | stats: {stats}')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('url', nargs='?', default='https://animesorion.cc/animes/')
    ap.add_argument('-o', '--output', default='orion.json')
    ap.add_argument('--firebase-key', default=None)
    ap.add_argument('--batch', type=int, default=100)
    ap.add_argument('--pages-limit', type=int, default=0)
    ap.add_argument('--no-skip', action='store_true')
    args = ap.parse_args()
    asyncio.run(pipeline(args.url, args.output,
                         firebase_key=args.firebase_key,
                         skip_existing=not args.no_skip,
                         batch=args.batch, pages_limit=args.pages_limit))

if __name__ == '__main__':
    main()
