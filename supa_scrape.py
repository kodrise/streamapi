#!/usr/bin/env python3
# language: Python, file: supa_scrape.py
# pipeline de scraping gravando direto no Supabase
import asyncio, json, os, re, sys, argparse, time
from urllib.parse import urlparse
from datetime import datetime, timezone
from dotenv import load_dotenv

# reusa do scrape.py tudo que nao depende do banco
from scrape import (
    _json_default, iso_now, embed_token_id, UA, log,
    listar, extrair_detalhes,
    capturar_embed, validar_token_blogger,
)

from supabase import create_client
from playwright.async_api import async_playwright

# carrega .env
load_dotenv()
SB = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])

def sb_anime_existe(slug):
    try:
        r = SB.table('animes').select('id,sources').eq('slug', slug).limit(1).execute()
        return r.data[0] if r.data else None
    except Exception:
        return None


def sb_push_anime(anime):
    """Upsert do anime pelo slug. Retorna o id."""
    slug = anime.get('id') or anime.get('slug')
    if not slug: return None
    row = {
        'slug': slug,
        'titulo': anime.get('titulo') or slug,
        'titulo_original': anime.get('titulo_original'),
        'capa': anime.get('capa'),
        'sinopse': anime.get('sinopse'),
        'ano': anime.get('ano'),
        'nota': anime.get('nota'),
        'status': anime.get('status'),
        'generos': anime.get('generos') or [],
        'tipo': anime.get('tipo'),
        'audio': anime.get('audio') or [],
        'sources': anime.get('sources') or ([anime['source']] if anime.get('source') else []),
        'episodes_count': anime.get('episodes_count') or len(anime.get('episodios', [])),
        'scraped_at': anime.get('scraped_at') or iso_now(),
    }
    SB.table('animes').upsert(row, on_conflict='slug').execute()
    r = SB.table('animes').select('id').eq('slug', slug).single().execute()
    return r.data['id'] if r.data else None

def sb_push_ep(anime_id, ep, source):
    """Upsert do ep e da fonte. Retorna episode_id."""
    numero = ep.get('numero')
    if numero is None: return None
    ep_row = {
        'anime_id': anime_id,
        'numero': numero,
        'titulo': ep.get('titulo'),
        'status': ep.get('status') or 'unknown',
    }
    import time

    def _com_retry(fn, tentativas=3, base=1.0):
        ultimo = None
        for t in range(tentativas):
            try:
                return fn()
            except Exception as e:
                ultimo = e
                time.sleep(base * (t + 1))
        raise ultimo

    _com_retry(lambda: SB.table('episodes').upsert(
        ep_row, on_conflict='anime_id,numero').execute())

    r = _com_retry(lambda: SB.table('episodes').select('id')\
        .eq('anime_id', anime_id).eq('numero', numero).single().execute())
    episode_id = r.data['id'] if r.data else None
    if not episode_id: return None

    src_row = {
        'episode_id': episode_id,
        'source': source,
        'url': ep.get('url'),
        'embed_url': ep.get('embed_url'),
        'embed_id': ep.get('embed_id'),
        'embed_type': ep.get('embed_type'),
        'status': ep.get('status') or 'unknown',
        'checked_at': iso_now(),
    }
    _com_retry(lambda: SB.table('episode_sources').upsert(
        src_row, on_conflict='episode_id,source').execute())
    return episode_id

def sb_add_source(anime_id, source):
    """Adiciona a fonte no array sources[] do anime se nao existir."""
    try:
        r = SB.table('animes').select('sources').eq('id', anime_id).single().execute()
        fontes = list((r.data or {}).get('sources') or [])
        if source not in fontes:
            fontes.append(source)
            SB.table('animes').update({'sources': fontes, 'updated_at': iso_now()})\
                .eq('id', anime_id).execute()
    except Exception:
        pass

async def pipeline(base_url, batch=100, pages_limit=0,
                   workers_detail=8, workers_embed=8, skip_existing=True):
    """Pipeline completo: lista -> detalhes -> embeds -> Supabase."""
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(user_agent=UA, viewport={'width':1920,'height':1080}, locale='pt-BR')

        if re.search(r'/anime/[^/]+/?$', base_url):
            log('[1] URL de anime especifico, pulando listagem')
            animes = [{'url': base_url, 'capa': None}]
        else:
            animes = await listar(ctx, base_url, pages_limit=pages_limit)
        log(f'[=] {len(animes)} animes na fila')

        resultado = [{'url': a['url'], 'capa': a.get('capa')} for a in animes]
        fonte = urlparse(base_url).netloc
        stats = {'d_ok':0,'d_skip':0,'e_ok':0,'e_dead':0,'e_sem':0}

        sem2 = asyncio.Semaphore(workers_detail)
        sem3 = asyncio.Semaphore(workers_embed)

        async def detalhe(i, item):
            async with sem2:
                slug = urlparse(item['url']).path.strip('/').split('/')[-1]
                existente = sb_anime_existe(slug)
                if skip_existing and existente and fonte in (existente.get('sources') or []):
                    stats['d_skip'] += 1
                    log(f'[2] {i+1}/{len(animes)} SKIP {slug}')
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
                    info['source'] = fonte
                    info['scraped_at'] = iso_now()
                    resultado[i].update(info)
                    stats['d_ok'] += 1
                    log(f'[2] {i+1}/{len(animes)} {slug} | {str(info.get("titulo"))[:48]} ({len(info.get("episodios",[]))} eps)')
                    aid = sb_push_anime(resultado[i])
                    if aid: sb_add_source(aid, fonte)
                    resultado[i]['_pg_id'] = aid
                except Exception as e:
                    log(f'[2] {i+1}/{len(animes)} ERRO: {e}')
                finally:
                    await pg.close()

        async def embed_ep(ai, ei, ep):
            async with sem3:
                aid = resultado[ai].get('_pg_id')
                if not aid:
                    stats['e_sem'] += 1; return
                numero = ep.get('numero') or (ei+1)
                # checa se esse ep+fonte ja existe com embed
                try:
                    r = SB.table('episode_sources').select('id')\
                        .eq('source', fonte)\
                        .eq('embed_url', ep.get('embed_url') or '___')\
                        .limit(1).execute()
                    if skip_existing and r.data:
                        return
                except Exception:
                    pass

                embed, tipo = await capturar_embed(ctx, ep['url'])
                d = resultado[ai]['episodios'][ei]
                d['embed_url'] = embed
                d['embed_type'] = tipo
                d['embed_id'] = embed_token_id(embed)
                d['id'] = f'ep-{numero:03d}'
                d['numero'] = numero
                d['scraped_at'] = iso_now()

                if embed:
                    d['status'] = 'unknown'
                else:
                    d['status'] = 'no_embed'

                if d['status'] == 'alive': stats['e_ok'] += 1
                elif d['status'] == 'dead': stats['e_dead'] += 1
                else: stats['e_sem'] += 1

                log(f'[3] {d["status"]} {resultado[ai].get("id")} ep{numero}')

                try:
                    await asyncio.to_thread(sb_push_ep, aid, d, fonte)
                except Exception as e:
                    log(f'  [sb erro ep] {e}')

        async def atualiza_count(ai):
            try:
                aid = resultado[ai].get('_pg_id')
                if not aid: return
                n = len(resultado[ai].get('episodios', []))
                await asyncio.to_thread(
                    lambda: SB.table('animes').update({'episodes_count': n}).eq('id', aid).execute())
            except Exception:
                pass

        async def processar_lote(idx, lote, total=None):
            tot = f'/{total}' if total else ''
            log(f'\n[===] LOTE {idx}{tot} ({len(lote)}) ===')
            await asyncio.gather(*(detalhe(i, it) for i, it in lote))
            tarefas = []
            for i, _ in lote:
                for ei, ep in enumerate(resultado[i].get('episodios', [])):
                    tarefas.append(embed_ep(i, ei, ep))
            for k in range(0, len(tarefas), workers_embed*2):
                await asyncio.gather(*tarefas[k:k+workers_embed*2])
            for i, _ in lote:
                await atualiza_count(i)
            log(f'[===] LOTE {idx}{tot} pronto | {stats}')

        pares = list(enumerate(resultado))
        tam = batch or len(pares)
        total_lotes = (len(pares)+tam-1)//tam
        prog = {'processados': 0, 'inicio': time.time()}

        log(f'\n[===] PROCESSANDO {len(pares)} ANIMES EM {total_lotes} LOTES DE {tam} ===')

        for idx in range(total_lotes):
            lote = pares[idx*tam:(idx+1)*tam]
            t0 = time.time()
            try:
                await processar_lote(idx+1, lote, total=total_lotes)
                prog['processados'] += len(lote)
                dur_lote = time.time() - t0
                dur_total = time.time() - prog['inicio']
                faltam = len(pares) - prog['processados']
                if prog['processados'] > 0:
                    eta_seg = (dur_total / prog['processados']) * faltam
                    eta_txt = f'{eta_seg/60:.0f}min' if eta_seg < 3600 else f'{eta_seg/3600:.1f}h'
                else:
                    eta_txt = '?'
                log(
                    f'[===] LOTE {idx+1}/{total_lotes} OK '
                    f'| processados: {prog["processados"]}/{len(pares)} '
                    f'| faltam: {faltam} '
                    f'| lote em {dur_lote:.0f}s '
                    f'| total {dur_total/60:.0f}min '
                    f'| ETA {eta_txt}'
                )
            except Exception as e:
                log(f'[!] lote {idx+1} falhou: {e} — continuando')
                prog['processados'] += len(lote)
                continue

        await b.close()
    log(f'\n[=] PRONTO | stats: {stats}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('url', nargs='?', default='https://goyabu.io/lista-de-animes')
    ap.add_argument('--batch', type=int, default=100)
    ap.add_argument('--pages-limit', type=int, default=0)
    ap.add_argument('--workers-detail', type=int, default=8)
    ap.add_argument('--workers-embed', type=int, default=8)
    ap.add_argument('--no-skip', action='store_true')
    args = ap.parse_args()
    asyncio.run(pipeline(args.url, batch=args.batch, pages_limit=args.pages_limit,
                         workers_detail=args.workers_detail, workers_embed=args.workers_embed,
                         skip_existing=not args.no_skip))


if __name__ == '__main__':
    main()
