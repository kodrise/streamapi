#!/usr/bin/env python3
# language: Python, file: validar_supa.py
# Testa embed_url dos eps contra o Blogger. Marca status alive/dead no Supabase.
import asyncio, os, re, sys, argparse
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client
from playwright.async_api import async_playwright

load_dotenv()
SB = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def log(*a): print(*a, flush=True)

async def validar_token(ctx, embed_url, timeout=20):
    """Retorna True (vivo), False (morto), None (skip)."""
    if not embed_url or "blogger.com" not in embed_url:
        return None
    pg = await ctx.new_page()
    achou = {"ok": False}
    async def on_resp(r):
        if "googlevideo.com/videoplayback" in r.url:
            achou["ok"] = True
        if "batchexecute" in r.url:
            try:
                body = await r.text()
                if "googlevideo" in body:
                    achou["ok"] = True
            except Exception:
                pass
    pg.on("response", on_resp)
    try:
        await pg.goto(embed_url, wait_until="domcontentloaded", timeout=timeout*1000)
        for _ in range(timeout):
            if achou["ok"]: break
            await pg.wait_for_timeout(1000)
    except Exception:
        pass
    finally:
        await pg.close()
    return achou["ok"]

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max sources (0=todos)")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--only-unknown", action="store_true", help="so os sem status")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    args = ap.parse_args()

    log("[*] coletando sources...")
    rows = []
    offset = 0
    page = 1000
    while True:
        q = SB.table("episode_sources").select("id,embed_url,status")\
            .not_.is_("embed_url", "null")
        if args.only_unknown:
            q = q.or_("status.is.null,status.eq.unknown")
        q = q.range(offset, offset + page - 1)
        lote = q.execute().data or []
        if not lote: break
        rows.extend(lote)
        if len(lote) < page: break
        offset += page
        if args.limit and len(rows) >= args.limit:
            rows = rows[:args.limit]; break

    # filtro de shard
    if args.of > 1:
        rows = [r for r in rows if (r["id"] % args.of) == args.shard]
    if args.limit: rows = rows[:args.limit]
    log(f"[*] {len(rows)} sources pra validar (shard {args.shard}/{args.of})\n")

    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await b.new_context(user_agent=UA, viewport={"width":1280,"height":720},
                                   locale="pt-BR",
                                   extra_http_headers={"Referer":"https://www.blogger.com/"})
        sem = asyncio.Semaphore(args.workers)
        c = {"ok":0, "dead":0, "err":0}

        async def worker(row):
            async with sem:
                try:
                    r = await validar_token(ctx, row["embed_url"])
                    if r is True:
                        st = "alive"; c["ok"] += 1
                    elif r is False:
                        st = "dead"; c["dead"] += 1
                    else:
                        return
                    SB.table("episode_sources").update({
                        "status": st,
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("id", row["id"]).execute()
                except Exception as e:
                    c["err"] += 1
                    log(f"  [erro] {str(e)[:100]}")

        tarefas = [worker(r) for r in rows]
        for i in range(0, len(tarefas), args.workers * 3):
            await asyncio.gather(*tarefas[i:i+args.workers*3])
            done = min(i + args.workers*3, len(tarefas))
            log(f"  {done}/{len(tarefas)} | alive:{c['ok']} dead:{c['dead']} err:{c['err']}")
        await b.close()

    log(f"\n[+] fim: {c['ok']} alive | {c['dead']} dead | {c['err']} erro")

if __name__ == "__main__":
    asyncio.run(main())
