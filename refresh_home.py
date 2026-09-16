#!/usr/bin/env python3
# language: Python, file: refresh_home.py
# Le a home do Goyabu e processa SO os eps recentes. Roda via cron.
import asyncio, os, re, sys
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from supabase import create_client
from playwright.async_api import async_playwright

import scrape  # reusa extrair_detalhes + capturar_embed
from scrape import limpar_slug

load_dotenv()
SB = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HOME = "https://goyabu.io/inicio"

def log(*a): print(*a, flush=True)
def iso(): return datetime.now(timezone.utc).isoformat()

async def ler_home(ctx):
    """Retorna lista de {url, titulo_anime, numero, capa} dos eps recentes."""
    pg = await ctx.new_page()
    try:
        await pg.goto(HOME, wait_until="networkidle", timeout=45000)
        await pg.wait_for_timeout(4000)
        for _ in range(6):
            await pg.mouse.wheel(0, 4000)
            await pg.wait_for_timeout(500)
        soup = BeautifulSoup(await pg.content(), "lxml")
        eps = []
        vistos = set()
        for art in soup.find_all("article", class_="boxEP"):
            a = art.find("a", href=True)
            if not a: continue
            ep_url = urljoin(HOME, a["href"])
            if ep_url in vistos: continue
            vistos.add(ep_url)

            titulo = None
            t = art.select_one(".title")
            if t: titulo = t.get_text(strip=True)

            num = None
            ep_el = art.select_one(".ep-type b, .ep-type")
            if ep_el:
                m = re.search(r"(\d+)", ep_el.get_text())
                if m: num = int(m.group(1))

            capa = None
            fig = art.find("figure")
            if fig:
                capa = fig.get("data-thumb") or fig.get("data-src")

            eps.append({
                "ep_url": ep_url,
                "titulo_anime": titulo,
                "numero": num,
                "capa": capa,
            })
        return eps
    finally:
        await pg.close()

async def achar_anime_do_ep(ctx, ep_url):
    """Abre o ep e devolve {anime_slug, anime_url} do anime pai."""
    pg = await ctx.new_page()
    try:
        await pg.goto(ep_url, wait_until="networkidle", timeout=30000)
        await pg.wait_for_timeout(2000)
        soup = BeautifulSoup(await pg.content(), "lxml")
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if re.search(r"/anime/[^/]+/?$", h):
                url = urljoin(ep_url, h)
                slug = limpar_slug(urlparse(url).path.strip("/").split("/")[-1])
                return {"anime_slug": slug, "anime_url": url}
        return None
    finally:
        await pg.close()

async def processar_ep(ctx, ep_info):
    """Abre a pagina do anime, extrai detalhes e grava os eps que faltam."""
    pg = await ctx.new_page()
    try:
        await pg.goto(ep_info["anime_url"], wait_until="networkidle", timeout=45000)
        await pg.wait_for_timeout(2500)
        info = scrape.extrair_detalhes(await pg.content(), ep_info["anime_url"])
        info["id"] = ep_info["anime_slug"]
        info["slug"] = ep_info["anime_slug"]
        info["source"] = "goyabu.io"
        info["scraped_at"] = iso()
        return info
    finally:
        await pg.close()

def push_anime(info):
    slug = info.get("slug")
    if not slug: return False
    doc = {k: v for k, v in info.items() if k != "episodios"}
    doc["episodes_count"] = len(info.get("episodios", []))
    doc["sources"] = list(set((doc.get("sources") or []) + ["goyabu.io"]))
    existe = SB.table('animes').select('morto').eq('slug', slug).limit(1).execute()
    morto_flag = bool(existe.data and existe.data[0].get('morto'))

    SB.table("animes").upsert({
        "slug": slug,
        "morto": morto_flag,
        "titulo": info.get("titulo") or slug,
        "titulo_original": info.get("titulo_original"),
        "capa": info.get("capa"),
        "sinopse": info.get("sinopse"),
        "ano": info.get("ano"),
        "nota": info.get("nota"),
        "status": info.get("status"),
        "tipo": info.get("tipo"),
        "audio": info.get("audio") or [],
        "generos": info.get("generos") or [],
        "sources": doc["sources"],
        "episodes_count": doc["episodes_count"],
        "scraped_at": iso(),
    }, on_conflict="slug").execute()

    r = SB.table("animes").select("id").eq("slug", slug).single().execute()
    aid = r.data["id"]

    for ep in info.get("episodios", []):
        numero = ep.get("numero")
        if numero is None: continue
        SB.table("episodes").upsert({
            "anime_id": aid,
            "numero": numero,
            "titulo": ep.get("titulo") or f"Ep {numero}",
        }, on_conflict="anime_id,numero").execute()

        er = SB.table("episodes").select("id").eq("anime_id", aid).eq("numero", numero).single().execute()
        eid = er.data["id"]

        if ep.get("embed_url"):
            SB.table("episode_sources").upsert({
                "episode_id": eid,
                "source": "goyabu.io",
                "url": ep.get("url"),
                "embed_url": ep.get("embed_url"),
                "embed_id": ep.get("embed_id"),
                "embed_type": ep.get("embed_type"),
                "status": "unknown",
                "checked_at": iso(),
            }, on_conflict="episode_id,source").execute()
    return True

async def main():
    log(f"[home] lendo {HOME}")
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await b.new_context(user_agent=UA, viewport={"width":1920,"height":1080}, locale="pt-BR")

        eps = await ler_home(ctx)
        log(f"[home] {len(eps)} eps recentes")

        animes_vistos = {}
        for e in eps:
            info = await achar_anime_do_ep(ctx, e["ep_url"])
            if not info: continue
            slug = info["anime_slug"]
            if slug in animes_vistos: continue
            animes_vistos[slug] = info

        log(f"[home] {len(animes_vistos)} animes unicos")

        ok = 0
        for slug, info in animes_vistos.items():
            try:
                detalhes = await processar_ep(ctx, info)
                n = len(detalhes.get("episodios", []))
                if n == 0:
                    SB.table("animes").update({"sem_eps": True}).eq("slug", slug).execute()
                    log(f"  [sem_eps] {slug}")
                    continue
                push_anime(detalhes)

                # NOVO: garante episode_sources pros eps criados sem embed
                aid_row = SB.table("animes").select("id").eq("slug", slug).single().execute()
                aid = aid_row.data["id"] if aid_row.data else None
                novos_embeds = 0
                if aid:
                    for ep in detalhes.get("episodios", []):
                        numero = ep.get("numero")
                        if numero is None:
                            continue
                        er = SB.table("episodes").select("id")\
                            .eq("anime_id", aid).eq("numero", numero).limit(1).execute()
                        if not er.data:
                            continue
                        eid = er.data[0]["id"]
                        ja = SB.table("episode_sources").select("id")\
                            .eq("episode_id", eid).limit(1).execute().data
                        if ja:
                            continue
                        embed, tipo = await scrape.capturar_embed(ctx, ep["url"])
                        if not embed:
                            continue
                        SB.table("episode_sources").upsert({
                            "episode_id": eid,
                            "source": "goyabu.io",
                            "url": ep["url"],
                            "embed_url": embed,
                            "embed_type": tipo,
                            "embed_id": scrape.embed_token_id(embed),
                            "status": "unknown",
                            "checked_at": iso(),
                        }, on_conflict="episode_id,source").execute()
                        novos_embeds += 1

                ok += 1
                log(f"  [ok] {slug}: {n} eps ({novos_embeds} embeds novos)")
            except Exception as ex:
                log(f"  [erro] {slug}: {str(ex)[:80]}")

        await b.close()
        log(f"\n[home] {ok} animes atualizados")

if __name__ == "__main__":
    asyncio.run(main())
