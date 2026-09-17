#!/usr/bin/env python3
# language: Python, file: backfill_meta.py
# Backfill de thumb/episode_name/audio/scraped_at em TODOS os animes do goyabu.
# NAO re-captura embed — so atualiza metadados dos eps.
# Uso: python backfill_meta.py --workers 6 --batch 50
import asyncio, os, re, argparse
from urllib.parse import urlparse
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from supabase import create_client
from scrape import extrair_detalhes, UA, log, iso_now

load_dotenv()
SB = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])


def listar_todos_animes():
    """Le todos os animes com slug valido, paginado."""
    itens, offset = [], 0
    while True:
        r = SB.table("animes").select("id,slug,morto")\
            .not_.is_("slug", "null")\
            .range(offset, offset + 999).execute().data or []
        if not r:
            break
        for a in r:
            if a.get("morto"):
                continue
            itens.append(a)
        if len(r) < 1000:
            break
        offset += 1000
    return itens


def atualizar_eps(aid, eps_novos):
    """Faz UPDATE de thumb/episode_name/audio/scraped_at em eps existentes.
    Nao cria eps novos. Nao toca em eps sem mudanca."""
    if not eps_novos:
        return 0, 0

    # pega todos os eps do anime com os campos atuais
    eps_db = SB.table("episodes").select("id,numero,thumb,episode_name,audio,scraped_at")\
        .eq("anime_id", aid).execute().data or []
    if not eps_db:
        return 0, 0

    mapa_db = {e["numero"]: e for e in eps_db}
    atualizados, sem_mudanca = 0, 0

    for ep in eps_novos:
        numero = ep.get("numero")
        if numero is None or numero not in mapa_db:
            continue
        atual = mapa_db[numero]

        novo = {}
        if ep.get("thumb") and atual.get("thumb") != ep["thumb"]:
            novo["thumb"] = ep["thumb"]
        if ep.get("episode_name") and atual.get("episode_name") != ep["episode_name"]:
            novo["episode_name"] = ep["episode_name"]
        if ep.get("audio") and atual.get("audio") != ep["audio"]:
            novo["audio"] = ep["audio"]
        if ep.get("scraped_at") and atual.get("scraped_at") != ep["scraped_at"]:
            novo["scraped_at"] = ep["scraped_at"]

        if not novo:
            sem_mudanca += 1
            continue

        SB.table("episodes").update(novo).eq("id", atual["id"]).execute()
        atualizados += 1

    return atualizados, sem_mudanca


async def processar(ctx, anime, sem, stats):
    async with sem:
        slug = anime["slug"]
        url = f"https://goyabu.io/anime/{slug}"
        pg = await ctx.new_page()
        try:
            for tentativa in range(3):
                try:
                    await pg.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await pg.wait_for_timeout(2000)
                    html = await pg.content()
                    break
                except Exception as e:
                    if tentativa == 2:
                        raise
                    await asyncio.sleep(1.5 * (tentativa + 1))
            info = extrair_detalhes(html, url)
            eps = info.get("episodios") or []
            if not eps:
                stats["sem_eps"] += 1
                return

            at, sm = await asyncio.to_thread(atualizar_eps, anime["id"], eps)
            stats["animes_ok"] += 1
            stats["eps_atualizados"] += at
            stats["eps_sem_mudanca"] += sm

            if at > 0:
                log(f"[{stats['animes_ok']:>4}] {slug}: {at} eps atualizados ({len(eps)} no site)")

        except Exception as e:
            stats["erros"] += 1
            log(f"[ERRO] {slug}: {str(e)[:100]}")
        finally:
            await pg.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--batch", type=int, default=50, help="log de progresso a cada N animes")
    ap.add_argument("--skip", type=int, default=0, help="pula os primeiros N animes (resume)")
    ap.add_argument("--limit", type=int, default=0, help="processa no maximo N animes")
    ap.add_argument("--shard", type=int, default=0, help="indice do shard (0-based)")
    ap.add_argument("--of", type=int, default=1, help="total de shards")
    args = ap.parse_args()

    animes = listar_todos_animes()

    if args.of > 1:
        animes = [a for a in animes if (a["id"] % args.of) == args.shard]
        log(f"[*] shard {args.shard}/{args.of}: {len(animes)} animes")

    if args.skip:
        animes = animes[args.skip:]
    if args.limit:
        animes = animes[:args.limit]

    log(f"[*] {len(animes)} animes na fila ({args.workers} workers)")

    stats = {"animes_ok": 0, "sem_eps": 0, "erros": 0,
             "eps_atualizados": 0, "eps_sem_mudanca": 0}

    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await b.new_context(user_agent=UA, viewport={"width":1920,"height":1080}, locale="pt-BR")
        sem = asyncio.Semaphore(args.workers)

        for i in range(0, len(animes), args.batch):
            lote = animes[i:i + args.batch]
            await asyncio.gather(*(processar(ctx, a, sem, stats) for a in lote))
            log(f"[=] lote {i+len(lote)}/{len(animes)} | {stats}")

        await b.close()

    log(f"\n[=] PRONTO | {stats}")


if __name__ == "__main__":
    asyncio.run(main())
