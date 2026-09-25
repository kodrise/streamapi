#!/usr/bin/env python3
# language: Python, file: tioanime_supa.py
# Scraper tioanime.com → Supabase. Fonte PT-BR com embed YourUpload (não-Blogger).
# Listagem: /directorio paginado. Anime: /anime/{slug}. Ep: /ver/{slug}-{n}.
# O embed está exposto em `var videos = [...]` no HTML do ep.
import asyncio, os, re, json, argparse
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from supabase import create_client
from playwright.async_api import async_playwright

from scrape import iso_now, UA, log

load_dotenv()
SB = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

BASE = "https://tioanime.com"
FONTE = "tioanime.com"


def norm_titulo(t):
    import re as _re
    s = t or ""
    s = _re.sub(r"\s+(dublado|legendado|online|hd|completo)+$", "", s, flags=_re.I)
    s = s.lower()
    s = _re.sub(r"[^a-z0-9]+", "", s)
    return s


def carregar_mapa():
    por_slug = {}
    por_titulo = {}
    offset = 0
    while True:
        r = SB.table("animes").select("id,slug,titulo,sources").range(offset, offset + 999).execute().data or []
        if not r:
            break
        for a in r:
            por_slug[a["slug"]] = a
            nt = norm_titulo(a.get("titulo") or a["slug"])
            if nt and nt not in por_titulo:
                por_titulo[nt] = a
        if len(r) < 1000:
            break
        offset += 1000
    log(f"[mapa] {len(por_slug)} animes carregados")
    return por_slug, por_titulo


def achar_anime_existente(mapa, titulo, slug_raw):
    por_slug, por_titulo = mapa
    slug = slug_raw.lower().strip()

    # 1) slug exacto + variantes
    for cand in [slug, f"{slug}-dublado", f"{slug}-legendado"]:
        if cand in por_slug:
            return por_slug[cand], "slug"

    # 2) slug como prefixo (dogulwang casa com dogulwang-tomb-raider-king)
    for s_db, a in por_slug.items():
        if s_db.startswith(slug + "-") or s_db == slug:
            return a, "slug_prefixo"

    # 3) título normalizado
    nt = norm_titulo(titulo)
    if nt and nt in por_titulo:
        return por_titulo[nt], "titulo"

    # 4) título como prefixo
    if nt:
        for t_db, a in por_titulo.items():
            if t_db.startswith(nt) or nt.startswith(t_db):
                return a, "titulo_prefixo"

    return None, None


def anexar_source(aid, fonte):
    r = SB.table("animes").select("sources").eq("id", aid).single().execute()
    atuais = list((r.data or {}).get("sources") or [])
    if fonte in atuais:
        return False
    atuais.append(fonte)
    SB.table("animes").update({"sources": atuais, "updated_at": iso_now()}).eq("id", aid).execute()
    return True


async def listar_animes(ctx, pages_limit=0):
    """Lista /directorio. TioAnime pagina com ?pagina=N"""
    itens, vistos = [], set()
    pg = await ctx.new_page()
    page = 1
    while True:
        if pages_limit and page > pages_limit:
            break
        url = f"{BASE}/directorio" + (f"?pagina={page}" if page > 1 else "")
        log(f"[1] pag {page}")
        try:
            await pg.goto(url, wait_until="networkidle", timeout=45000)
            await pg.wait_for_timeout(2200)
            for _ in range(4):
                await pg.mouse.wheel(0, 4000); await pg.wait_for_timeout(400)
        except Exception as e:
            log(f"[1] erro: {e}")
            break
        soup = BeautifulSoup(await pg.content(), "lxml")
        novos = 0
        for a in soup.find_all("a", href=True):
            h = a["href"]
            m = re.search(r"/anime/([^/]+)/?$", h)
            if not m:
                continue
            link = urljoin(BASE, h)
            slug_raw = m.group(1)
            if link in vistos:
                continue
            vistos.add(link)
            capa = None
            img = a.find("img")
            if img:
                capa = img.get("src") or img.get("data-src")
                if capa:
                    capa = urljoin(BASE, capa)
            itens.append({"url": link, "slug_raw": slug_raw, "capa": capa})
            novos += 1
        log(f"[1] pag {page}: +{novos} (total {len(itens)})")
        if novos == 0:
            break
        page += 1
    await pg.close()
    return itens


def extrair_detalhes(html, url):
    soup = BeautifulSoup(html, "lxml")
    out = {"url": url, "titulo": None, "capa": None, "sinopse": None,
           "ano": None, "generos": [], "episodios": []}

    h1 = soup.find("h1")
    if h1:
        out["titulo"] = h1.get_text(" ", strip=True)

    # capa — TioAnime usa /uploads/portadas/{id}.jpg
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "/uploads/portadas/" in src:
            out["capa"] = urljoin(url, src)
            break
    if not out["capa"]:
        meta = soup.find("meta", property="og:image")
        if meta:
            out["capa"] = meta.get("content")

    # sinopse
    for sel in [".sinopsis", ".description", ".synopsis", ".anime-synopsis"]:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(" ", strip=True)
            if len(txt) > 40:
                out["sinopse"] = txt; break

    # ano
    m = re.search(r"\b(19[89]\d|20[0-2]\d)\b", soup.get_text())
    if m:
        out["ano"] = int(m.group(0))

    # generos
    for a in soup.select('a[href*="/genero/"]'):
        g = a.get_text(strip=True)
        if g and g not in out["generos"] and len(g) < 40:
            out["generos"].append(g)

    # eps: /ver/{slug}-{n}
    vistos = set()
    for a in soup.find_all("a", href=True):
        h = a["href"]
        m = re.search(r"/ver/[^/]+-(\d+)/?$", h)
        if not m:
            continue
        ep_url = urljoin(url, h)
        if ep_url in vistos:
            continue
        vistos.add(ep_url)
        num = int(m.group(1))
        out["episodios"].append({
            "numero": num,
            "titulo": f"Ep {num}",
            "url": ep_url,
        })
    out["episodios"].sort(key=lambda e: e["numero"])
    return out


def extrair_embed(html):
    """O TioAnime expõe os players em `var videos = [["Mega","url",0,0], ...]`"""
    m = re.search(r'var\s+videos\s*=\s*(\[[\s\S]*?\])\s*;', html)
    if not m:
        return None
    try:
        raw = m.group(1)
        # JSON dos players (com \/ escapado)
        players = json.loads(raw.replace("\\/", "/"))
    except Exception:
        return None
    # prioridade: YourUpload > Voe > Mega
    prioridade = {"yourupload": 1, "voe": 2, "mega": 3}
    players.sort(key=lambda p: prioridade.get(p[0].lower(), 99))
    for p in players:
        nome, url = p[0], p[1]
        if url and url.startswith("http"):
            return {"source": nome.lower(), "embed_url": url}
    return None


async def processar_ep(ctx, aid, ep, embed_info, semaphore):
    async with semaphore:
        if not embed_info:
            return "sem"

        # upsert do ep (cria se não existir)
        SB.table("episodes").upsert({
            "anime_id": aid, "numero": ep["numero"],
            "titulo": ep["titulo"], "status": "unknown",
        }, on_conflict="anime_id,numero").execute()

        eid_row = SB.table("episodes").select("id").eq("anime_id", aid).eq("numero", ep["numero"]).single().execute()
        if not eid_row.data:
            return "sem"
        eid = eid_row.data["id"]

        # já existe essa source?
        ja = SB.table("episode_sources").select("id").eq("episode_id", eid).eq("source", FONTE).limit(1).execute().data
        if ja:
            return "skip"

        SB.table("episode_sources").upsert({
            "episode_id": eid,
            "source": FONTE,
            "url": ep["url"],
            "embed_url": embed_info["embed_url"],
            "embed_type": embed_info["source"],
            "status": "unknown",
            "checked_at": iso_now(),
        }, on_conflict="episode_id,source").execute()
        return "novo"


async def pipeline(batch=20, pages_limit=0, workers_detail=4, workers_embed=6):
    mapa = carregar_mapa()

    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await b.new_context(user_agent=UA, viewport={"width": 1920, "height": 1080}, locale="pt-BR")

        animes = await listar_animes(ctx, pages_limit=pages_limit)
        log(f"[=] {len(animes)} animes na fila")

        sem_det = asyncio.Semaphore(workers_detail)
        sem_emb = asyncio.Semaphore(workers_embed)
        stats = {"novos": 0, "enriquecidos": 0, "erro": 0, "ep_novo": 0, "ep_skip": 0, "ep_sem": 0}

        async def detalhe(item):
            async with sem_det:
                pg = await ctx.new_page()
                try:
                    await pg.goto(item["url"], wait_until="networkidle", timeout=45000)
                    await pg.wait_for_timeout(2200)
                    for _ in range(4):
                        await pg.mouse.wheel(0, 4000); await pg.wait_for_timeout(400)
                    info = extrair_detalhes(await pg.content(), item["url"])

                    if not info.get("episodios"):
                        return
                    slug_limpo = item["slug_raw"].lower().strip()
                    existente, como = achar_anime_existente(mapa, info.get("titulo"), item["slug_raw"])

                    if existente:
                        aid = existente["id"]
                        anexar_source(aid, FONTE)
                        stats["enriquecidos"] += 1
                        log(f"[2] {item['slug_raw']} → MATCH({como}) {existente['slug']} ({len(info['episodios'])} eps)")
                    else:
                        # sem match: cria novo com o título do TioAnime.
                        # Pode duplicar animes que existem noutro idioma — aceitável
                        # porque a cobertura importa mais que o catálogo perfeito.
                        SB.table("animes").upsert({
                            "slug": slug_limpo,
                            "titulo": info.get("titulo") or item["slug_raw"],
                            "capa": info.get("capa"),
                            "sinopse": info.get("sinopse"),
                            "ano": info.get("ano"),
                            "generos": info.get("generos") or [],
                            "sources": [FONTE],
                            "episodes_count": len(info["episodios"]),
                            "scraped_at": iso_now(),
                        }, on_conflict="slug").execute()
                        r = SB.table("animes").select("id").eq("slug", slug_limpo).single().execute()
                        aid = r.data["id"]
                        mapa[0][slug_limpo] = {"id": aid, "slug": slug_limpo, "titulo": info.get("titulo"), "sources": [FONTE]}
                        stats["novos"] = stats.get("novos", 0) + 1
                        log(f"[2] {item['slug_raw']} → NOVO (id {aid}) ({len(info['episodios'])} eps)")

                    # processar eps em paralelo
                    for ep in info["episodios"]:
                        pg_ep = await ctx.new_page()
                        try:
                            await pg_ep.goto(ep["url"], wait_until="domcontentloaded", timeout=30000)
                            await pg_ep.wait_for_timeout(2500)
                            html_ep = await pg_ep.content()
                            embed_info = extrair_embed(html_ep)
                            r = await processar_ep(ctx, aid, ep, embed_info, sem_emb)
                            if r == "novo":
                                stats["ep_novo"] += 1
                                log(f"    ep{ep['numero']} → {embed_info['source']}")
                            elif r == "skip":
                                stats["ep_skip"] += 1
                            else:
                                stats["ep_sem"] += 1
                                log(f"    ep{ep['numero']} → SEM EMBED")
                        except Exception:
                            stats["ep_sem"] += 1
                        finally:
                            await pg_ep.close()
                except Exception as e:
                    stats["erro"] += 1
                    log(f"[2] erro: {str(e)[:100]}")
                finally:
                    await pg.close()

        for i in range(0, len(animes), batch):
            lote = animes[i:i + batch]
            await asyncio.gather(*(detalhe(it) for it in lote))
            log(f"[=] lote {i + len(lote)}/{len(animes)} | {stats}")

        await b.close()
    log(f"\n[=] PRONTO | {stats}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--pages-limit", type=int, default=0)
    ap.add_argument("--workers-detail", type=int, default=4)
    ap.add_argument("--workers-embed", type=int, default=6)
    args = ap.parse_args()
    asyncio.run(pipeline(batch=args.batch, pages_limit=args.pages_limit,
                         workers_detail=args.workers_detail,
                         workers_embed=args.workers_embed))


if __name__ == "__main__":
    main()
