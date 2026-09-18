#!/usr/bin/env python3
# language: Python, file: scrape.py
# scraper + Firestore em lotes, num arquivo so
# uso: python scrape.py "https://goyabu.io/lista-de-animes" -o catalogo.json --firebase-key serviceAccountKey.json

import asyncio, json, re, sys, argparse, os, time
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# ============ FIREBASE INLINE ============
# firebase-admin e opcional — so carrega se estiver instalado
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    _FIREBASE_OK = True
except ImportError:
    firebase_admin = None
    credentials = None
    firestore = None
    _FIREBASE_OK = False

_fb = {'db': None, 'on': False}

def fb_init(key_path):
    if _fb['db'] is None:
        firebase_admin.initialize_app(credentials.Certificate(key_path))
        _fb['db'] = firestore.client()
    _fb['on'] = True
    return _fb['db']

def fb_push_anime(anime):
    """1 write por anime — eps em array dentro do doc pai."""
    if not _fb['on']: return False, 'off'
    aid = anime.get('id') or anime.get('slug')
    if not aid: return False, 'sem id'
    eps = anime.get('episodios', anime.get('episodes', []))
    ref = _fb['db'].collection('animes').document(aid)
    doc = {k: v for k, v in anime.items() if k not in ('episodios', 'episodes')}
    doc['episodes_count'] = len(eps)
    # eps embutidos como array no doc pai
    eps_limpos = []
    for ep in eps:
        e = dict(ep)
        e['id'] = e.get('id') or f"ep-{e.get('numero') or 0:03d}"
        eps_limpos.append(e)
    doc['episodes'] = eps_limpos
    doc['updated_at'] = firestore.SERVER_TIMESTAMP
    ref.set(doc, merge=True)
    return True, aid

def fb_push_ep(anime_id, ep, source=None):
    """No-op — economiza writes. O estagio 3 usa fb_push_anime."""
    if not _fb['on']: return False, 'off'
    return True, ep.get('id') or '?'


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

def fb_mark_full_scan(total):
    if not _fb['on']: return
    try:
        _fb['db'].collection('_meta').document('full_scan').set({
            'done': True, 'total_animes': total, 'finished_at': iso_now(),
            'updated_at': firestore.SERVER_TIMESTAMP
        }, merge=True)
    except Exception:
        pass

def fb_full_scan_done(threshold=500):
    if not _fb['on']: return False
    try:
        doc = _fb['db'].collection('_meta').document('full_scan').get()
        if doc.exists and (doc.to_dict() or {}).get('done'):
            return True
    except Exception:
        pass
    try:
        count = 0
        for _ in _fb['db'].collection('animes').limit(threshold + 1).stream():
            count += 1
        return count > threshold
    except Exception:
        return False


def _json_default(o):
    if hasattr(o, 'isoformat'):
        return o.isoformat()
    if hasattr(o, 'to_dict'):
        try: return o.to_dict()
        except Exception: pass
    return str(o)


def fb_source_done(aid, source):
    """True se a fonte ja contribuiu pro anime (esta em sources[])."""
    if not _fb['on'] or not aid or not source: return False
    try:
        doc = _fb['db'].collection('animes').document(aid).get()
        if not doc.exists: return False
        d = doc.to_dict() or {}
        return source in (d.get('sources') or [])
    except Exception:
        return False

# ============ HELPERS ============
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

def log(*a): print(*a, flush=True)
def iso_now(): return datetime.now(timezone.utc).isoformat()

CYR = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z',
    'и':'i','й':'i','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
    'с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts','ч':'ch','ш':'sh','щ':'sch',
    'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
    'А':'a','Б':'b','В':'v','Г':'g','Д':'d','Е':'e','Ё':'e','Ж':'zh','З':'z',
    'И':'i','Й':'i','К':'k','Л':'l','М':'m','Н':'n','О':'o','П':'p','Р':'r',
    'С':'s','Т':'t','У':'u','Ф':'f','Х':'h','Ц':'ts','Ч':'ch','Ш':'sh','Щ':'sch',
    'Ъ':'','Ы':'y','Ь':'','Э':'e','Ю':'yu','Я':'ya',
}


def limpar_slug(slug):
    """Normaliza slug URL-encoded/Unicode pra ASCII puro."""
    from urllib.parse import unquote
    import unicodedata
    s = unquote(slug)
    s = ''.join(CYR.get(ch, ch) for ch in s)
    s = unicodedata.normalize('NFKD', s)
    s = s.encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r'[^a-z0-9]+', '-', s.lower())
    s = re.sub(r'-+', '-', s).strip('-')
    return s


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

    # sinopse — .sinopse-full tem o texto completo (o _short e truncado)
    el = soup.select_one('.sinopse-full') or soup.select_one('.streamer-sinopse')
    if el:
        txt = el.get_text(' ', strip=True)
        titulo = out.get('titulo') or ''
        # remove SEO boilerplate
        patterns = [
            r'\b' + re.escape(titulo) + r'\s+Todos os Epis[oó]dios? Onl[^.?!]*[.?!]',
            r'\b' + re.escape(titulo) + r'\s+Anime Completo[,.]',
            r'\bAssistir\s+' + re.escape(titulo) + r'[^.?!]*[.?!]',
            r'Todos os Epis[oó]dios? Onl[^.?!]*[.?!]',
            r'Assistir [^.!?]{0,150}?(?:Completo|Online|Dublado|Legendado)[^.!?]*[.!?]',
            r'[^.?!]{0,200}?Todos os Epis[oó]dios? Onl[^.?!]*\.?',
            r'\s*ler mais\s*$',
        ]
        for p in patterns:
            txt = re.sub(p, ' ', txt, flags=re.I)

        # corte generico: se ainda tem boilerplate no inicio, corta ate o 1o "."
        while True:
            m = re.match(r'^[^.]{0,200}?(?:Anime Completo|Assistir|Online\.|Completo,)', txt, re.I)
            if not m:
                break
            # acha o primeiro ponto-e-espaco depois do boilerplate
            m2 = re.search(r'\.\s+', txt[m.end():])
            if not m2:
                break
            txt = txt[m.end() + m2.end():]
            break

        txt = re.sub(r'\s+', ' ', txt).strip()
        txt = re.sub(r'^[.,;:\-\s]+', '', txt)
        txt = re.sub(r'[.,;:\-\s]+$', '', txt)
        if len(txt) > 40:
            out['sinopse'] = txt

    for sel in ['.description', '.sinopse', '.wp-content p', '.info .desc', '.desc']:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(' ', strip=True)
            if len(txt) > 40: out['sinopse'] = txt; break
    if not out['sinopse']:
        meta = soup.find('meta', property='og:description') or soup.find('meta', attrs={'name':'description'})
        if meta: out['sinopse'] = meta.get('content','').strip()

    # ano — restringe a 1990-2029 e prioriza proximidade com o titulo
    candidatos = []
    for sel in ['[itemprop="datePublished"]', '.year', '.ano', '.release-year', '.data-ano', '.lancamento']:
        el = soup.select_one(sel)
        if el:
            for m in re.finditer(r'\b(19[89]\d|20[0-2]\d)\b', el.get_text()):
                candidatos.append(int(m.group(0)))
    if not candidatos:
        # fallback: procura por "Lançado em <mes> <ano>" ou "Ano: 2024"
        txt = soup.get_text()
        m = re.search(r'(?:Lan[çc]ado em|Ano|Estreia)\D{0,20}(20[0-2]\d|19[89]\d)', txt)
        if m: candidatos.append(int(m.group(1)))
    if candidatos:
        out['ano'] = max(candidatos)

    # OVERRIDE: .streamer-info tem ano e nota reais do Goyabu
    info_el = soup.select_one('.streamer-info')
    if info_el:
        info_txt = info_el.get_text(' ', strip=True)
        m_ano = re.search(r'HD\s+(19[89]\d|20[0-2]\d)', info_txt)
        if m_ano:
            out['ano'] = int(m_ano.group(1))
        m_nota = re.search(r'(\d+\.\d+)\s+\d*\s*votos', info_txt)
        if m_nota:
            v = float(m_nota.group(1))
            if 0 < v <= 10:
                out['nota'] = v  # o mais recente eh o mais provavel

    # nota — mais seletores + valida 0-10
    for sel in ['.rating-poster', '.rating', '.nota', '[itemprop="ratingValue"]', '.score',
                '.dt_rating', '.rating-score', '.rating-score-box', '.starstruck-rating',
                '.average', '.vote-average', '.imdb', '.nota-anime', '.post-ratings']:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text() + ' ' + (el.get('content') or '')
            m = re.search(r'(\d+(?:\.\d+)?)', txt)
            if m:
                v = float(m.group(1))
                if 0 < v <= 10:
                    out['nota'] = v; break
    if not out['nota']:
        # ultimo recurso: busca "Nota: X.X" ou "Rating: X.X" no HTML
        m = re.search(r'(?:Nota|Rating|Score)[:\s]+(\d+(?:\.\d+)?)', soup.get_text(), re.I)
        if m:
            v = float(m.group(1))
            if 0 < v <= 10: out['nota'] = v

    for sel in ['.status', '.estado', '.situacao']:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            out['status'] = el.get_text(strip=True); break

    # generos — Goyabu usa /generos/{slug} (plural)
    for a in soup.select('a[href*="/generos/"], a[href*="/genero/"], .genres a, .sgeneros a, .generos a'):
        g = a.get_text(strip=True)
        href = a.get('href', '')
        # ignora o link "Categorias" (raiz /generos sem slug)
        if re.search(r'/generos?/?$', href): continue
        if g and g not in out['generos'] and len(g) < 40:
            out['generos'].append(g)
            # guarda o slug tambem
            m = re.search(r'/generos?/([^/]+)/?', href)
            if m:
                out.setdefault('generos_slugs', {})[g] = m.group(1)

    # tipo — Goyabu nao expoe; usa heuristica
    for sel in ['.typez', '.ep-type b']:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(strip=True).upper()
            if t in ('TV', 'FILME', 'MOVIE', 'OVA', 'ONA', 'ESPECIAL', 'SPECIAL'):
                out['tipo'] = t.capitalize()
                break
    if not out.get('tipo'):
        titulo = (out.get('titulo') or '').lower()
        if any(x in titulo for x in [' filme', ' movie']):
            out['tipo'] = 'Filme'
        elif ' ova' in titulo:
            out['tipo'] = 'OVA'
        elif ' ona' in titulo:
            out['tipo'] = 'ONA'
        else:
            out['tipo'] = 'TV'

    # audio — seletor real do Goyabu: .audio-box.legendado / .audio-box.dublado
    audio = []
    for a_box in soup.select('.audio-box'):
        classes = ' '.join(a_box.get('class') or []).lower()
        if 'dublado' in classes and 'dublado' not in audio:
            audio.append('dublado')
        if 'legendado' in classes and 'legendado' not in audio:
            audio.append('legendado')
    titulo_l = (out.get('titulo') or '').lower()
    if 'dublado' in titulo_l and 'dublado' not in audio:
        audio.append('dublado')
    if not audio:
        audio.append('legendado')
    out['audio'] = audio

    # tenta allEpisodes (JSON inline) primeiro — traz thumb, episode_name, data
    _eps_json = _parse_all_episodes(html, url)
    if _eps_json:
        out['episodios'] = _eps_json
        return out

    # fallback: parser DOM (animes sem allEpisodes)
    vistos = set()
    for a in soup.find_all('a', href=True):
        href = a['href']
        m_id = re.search(r'/(\d{3,8})/?$', href)
        m_ep = re.search(r'/(?:episodio|ep|epi)[-/]?(\d+)', href, re.I)
        if not (m_id or m_ep): continue
        if re.search(r'/(anime|genero|lista|perfil|login|calendario|random|populares|lancamentos|wp-|page)/', href): continue
        # heuristica extra: ignora links de share/social (facebook, twitter, whatsapp, telegram)
        if any(s in href for s in ['facebook.com', 'twitter.com', 'whatsapp', 't.me/', 'sharer']): continue
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

# ============ PARSER: allEpisodes (JSON inline) ============
def _parse_all_episodes(html, base_url):
    """
    Extrai o array allEpisodes do HTML inline do goyabu.
    Vantagens vs parser DOM:
      - thumb por ep (campo 'imagem')
      - episode_name (titulo real)
      - update (data ISO exata)
      - audio (ptBr/jap)
    Retorna lista de dicts ou None se nao achar.
    """
    m = re.search(r'allEpisodes\s*[:=]\s*(\[[^\]]+\])', html, re.S)
    if not m:
        return None
    try:
        raw = json.loads(m.group(1))
    except Exception:
        return None
    if not isinstance(raw, list):
        return None

    eps = []
    for e in raw:
        numero = e.get("episodio")
        if numero is None:
            continue
        try:
            numero = int(numero)
        except (ValueError, TypeError):
            continue

        thumb = None
        if e.get("imagem"):
            img = e["imagem"]
            thumb = img if img.startswith("http") else urljoin(base_url, img)

        link = e.get("link") or ""
        ep_url = link if link.startswith("http") else urljoin(base_url, link)

        name = e.get("episode_name") or None

        eps.append({
            "numero": numero,
            "titulo": name or f"Ep {numero}",
            "episode_name": name,
            "url": ep_url,
            "thumb": thumb,
            "audio": e.get("audio"),
            "scraped_at": e.get("update"),
        })
    return eps if eps else None


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

    # anivideo: token HLS expira em horas, nao serve pra gravar
    if embed and "api.anivideo.net" in embed:
        return None, None

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
                json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)

        sem2 = asyncio.Semaphore(workers_detail)
        sem3 = asyncio.Semaphore(workers_embed)
        stats = {'detalhe_skip': 0, 'detalhe_ok': 0, 'detalhe_erro': 0,
                 'embed_ok': 0, 'embed_skip': 0, 'embed_sem': 0, 'embed_dead': 0}

        async def detalhe(i, item):
            async with sem2:
                slug = urlparse(item['url']).path.strip('/').split('/')[-1]
                fonte = urlparse(item['url']).netloc
                if skip_existing and fb_anime_pronto(slug) and fb_source_done(slug, fonte):
                    stats['detalhe_skip'] += 1
                    log(f'[2] {i+1}/{len(animes)} SKIP {slug} (fonte {fonte} ja ok)')
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
                fonte = urlparse(resultado[ai].get('url') or '').netloc
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
                    d['status'] = 'unknown'
                    stats['embed_ok'] += 1
                    log(f'[3] ok {resultado[ai].get("id","?")} ep{ep.get("numero") or ei+1} -> {str(embed)[:50]}')
                else:
                    d['status'] = 'no_embed'
                    stats['embed_sem'] += 1
                    log(f'[3] SEM {resultado[ai].get("id","?")} ep{ep.get("numero") or ei+1}')
                if _fb['on']:
                    try:
                        await asyncio.to_thread(fb_push_anime, resultado[ai])
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
    ap.add_argument('--home-url', default=None)
    ap.add_argument('--mark-done', action='store_true')
    args = ap.parse_args()
    if args.mark_done:
        if not args.firebase_key:
            print('precisa --firebase-key'); return
        fb_init(args.firebase_key)
        try:
            n = len(list(_fb['db'].collection('animes').stream()))
        except Exception:
            n = 0
        fb_mark_full_scan(n)
        print(f'[+] varredura marcada como completa ({n} animes)')
        return

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
