#!/usr/bin/env python3
# language: Python, file: animesdigital_supa.py
# Scraper animesdigital.org — enriquece animes existentes com nova source.
# Casa por slug limpo ou titulo normalizado. NAO duplica anime.
# Por ep: cria se nao existe; adiciona source se < min_alive sources alive; pula se ja tem.
import asyncio, os, re, argparse, unicodedata, json
import httpx
from urllib.parse import urljoin
from datetime import datetime, timezone
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from supabase import create_client
from playwright.async_api import async_playwright

from scrape import (
    iso_now, embed_token_id, UA, log,
    capturar_embed, limpar_slug,
)

load_dotenv()
SB = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

BASE = "https://animesdigital.org"
LISTAGENS = [
    f"{BASE}/animes-dublado001?filter_letter=0&type_url=animes&filter_audio=dublado&filter_order=name",
    f"{BASE}/animes-legendados-online001?filter_letter=0&type_url=animes&filter_audio=legendado&filter_order=name",
]
FONTE = "animesdigital.org"


# ============ helpers ============
def norm_titulo(t):
    """Titulo normalizado: sem boilerplate (Assistir/Online/HD/Dublado),
    sem acento, sem pontuacao, lowercase."""
    import re as _re
    s = t or ""
    s = _re.sub(r"^\s*assistir\s+", "", s, flags=_re.I)
    s = _re.sub(
        r"\s+(dublado|legendado|online|hd|completo|em\s+hd|"
        r"online\s+em\s+hd|dublado\s+online|legendado\s+online|"
        r"dublado\s+online\s+em\s+hd|legendado\s+online\s+em\s+hd)+\s*$",
        "", s, flags=_re.I)
    s = _re.sub(
        r"\s+(dublado|legendado|online|hd|completo|em\s+hd)+\s*$",
        "", s, flags=_re.I)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return _re.sub(r"[^a-z0-9]+", "", s.lower())


def carregar_mapa_animes():
    """Baixa todos os animes do Supabase e monta indices de casamento."""
    por_slug = {}
    por_titulo = {}
    offset = 0
    total = 0
    while True:
        r = SB.table("animes").select("id,slug,titulo,sources")\
            .range(offset, offset + 999).execute().data or []
        if not r:
            break
        for a in r:
            por_slug[a["slug"]] = a
            nt = norm_titulo(a.get("titulo") or a["slug"])
            if nt and nt not in por_titulo:
                por_titulo[nt] = a
            total += 1
        if len(r) < 1000:
            break
        offset += 1000
    log(f"[mapa] {total} animes carregados ({len(por_titulo)} titulos unicos)")
    return por_slug, por_titulo


def _variantes_slug(slug):
    """Gera variantes do slug pra tentar casamento por slug exato."""
    variantes = [slug]
    # sufixos de audio
    for s in ("dublado", "legendado"):
        variantes.append(f"{slug}-{s}")
    # sufixos numericos (temporadas)
    m = re.search(r"^(.*?)-(\d+)$", slug)
    if m:
        base, num = m.group(1), m.group(2)
        variantes.append(f"{base}-{num}-dublado")
        variantes.append(f"{base}-{num}-legendado")
    return variantes


def achar_anime_existente(mapa, titulo, slug_raw):
    """Politica CONSERVADORA: so casa por slug exato + variantes.
    Titulo NAO e usado — evita falso positivo entre traducao/temporada.

    como = 'slug' | 'slug_audio' | 'slug_temporada'
    """
    por_slug, _ = mapa
    slug = limpar_slug(slug_raw)

    # 1) slug puro
    if slug in por_slug:
        return por_slug[slug], "slug"

    # 2+3) variantes (audio, temporada)
    for cand in _variantes_slug(slug)[1:]:
        if cand in por_slug:
            if cand.endswith("-dublado") or cand.endswith("-legendado"):
                return por_slug[cand], "slug_audio"
            return por_slug[cand], "slug_temporada"

    return None, None

def anexar_source(aid, fonte):
    """Adiciona a fonte ao array sources[] do anime se ainda nao existir."""
    r = SB.table("animes").select("sources").eq("id", aid).single().execute()
    atuais = list((r.data or {}).get("sources") or [])
    if fonte in atuais:
        return False
    atuais.append(fonte)
    SB.table("animes").update({
        "sources": atuais,
        "updated_at": iso_now(),
    }).eq("id", aid).execute()
    return True


# ============ ESTAGIO 1: LISTAR ============
async def listar_animes(ctx, listagem_base, pages_limit=0, filtros_extras=None):
    """
    Estratégia: o site faz o primeiro POST /func/listanime sozinho quando a
    página carrega. Interceptamos esse request, roubamos o token, e reusamos
    pra paginar via pg.evaluate.
    """
    from urllib.parse import urlparse

    pg = await ctx.new_page()
    itens, vistos = [], set()
    token_capturado = {"valor": None}
    primeira_resposta = {"body": None}

    def on_req(r):
        if "func/listanime" in r.url and r.method == "POST" and r.post_data:
            import re as _re
            m = _re.search(r"token=([a-f0-9]+)", r.post_data)
            if m and not token_capturado["valor"]:
                token_capturado["valor"] = m.group(1)

    async def on_resp(r):
        if "func/listanime" in r.url and primeira_resposta["body"] is None:
            try:
                primeira_resposta["body"] = await r.text()
            except Exception:
                pass

    pg.on("request", on_req)
    pg.on("response", on_resp)

    try:
        print(f"[1] abrindo listagem base (site fará o primeiro POST sozinho)")
        await pg.goto(listagem_base, wait_until="networkidle", timeout=45000)
        await pg.wait_for_timeout(4000)

        if not token_capturado["valor"]:
            log("[1] ERRO: site não fez POST /func/listanime — token não capturado")
            return []

        token = token_capturado["valor"]
        log(f"[1] token capturado do site: {token}")

        parsed = urlparse(listagem_base)
        base_origin = f"{parsed.scheme}://{parsed.netloc}"
        q = dict(pair.split("=", 1) for pair in (parsed.query or "").split("&") if "=" in pair)
        filter_data = "&".join([
            f"filter_letter={q.get('filter_letter', '0')}",
            f"type_url={q.get('type_url', 'animes')}",
            f"filter_audio={q.get('filter_audio', 'dublado')}",
            f"filter_order={q.get('filter_order', 'name')}",
        ])
        if filtros_extras:
            filter_data += "&" + filtros_extras

        async def parse_results_json(body):
            try:
                j = json.loads(body)
            except Exception:
                return 0, None
            results = j.get("results") or []
            total_page = j.get("total_page")
            novos = 0
            for html_item in results:
                soup = BeautifulSoup(html_item, "lxml")
                a = soup.find("a", href=True)
                if not a:
                    continue
                h = a["href"]
                m = re.search(r"/anime/([a-z0-9])/([^/]+)/?$", h, re.I)
                if not m:
                    continue
                if h in vistos:
                    continue
                vistos.add(h)
                capa = None
                img = soup.find("img")
                if img:
                    capa = img.get("src") or img.get("data-src")
                itens.append({"url": h, "slug_raw": m.group(2), "capa": capa})
                novos += 1
            return novos, total_page

        # pagina 1: reusa a resposta que o site já trouxe
        total_page = None
        if primeira_resposta["body"]:
            novos, total_page = await parse_results_json(primeira_resposta["body"])
            log(f"[1] pag 1/{total_page}: +{novos} (total {len(itens)}) [reusada]")

        # paginas 2+ via pg.evaluate com o token capturado
        pagina = 2
        while True:
            if pages_limit and pagina > pages_limit:
                break
            if total_page and pagina > total_page:
                break

            log(f"[1] POST pagina={pagina}")
            try:
                resultado = await pg.evaluate("""
                    async ({endpoint, pagina, token, filter_data}) => {
                        const filters = {
                            filter_data: filter_data,
                            filter_genre_add: [],
                            filter_genre_del: []
                        };
                        const body = new URLSearchParams();
                        body.append('token', token);
                        body.append('pagina', String(pagina));
                        body.append('search', '0');
                        body.append('limit', '30');
                        body.append('type', 'lista');
                        body.append('filters', JSON.stringify(filters));
                        const r = await fetch(endpoint, {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                                'X-Requested-With': 'XMLHttpRequest',
                            },
                            body: body.toString(),
                        });
                        return { status: r.status, body: await r.text() };
                    }
                """, {"endpoint": f"{base_origin}/func/listanime",
                      "pagina": pagina, "token": token,
                      "filter_data": filter_data})
            except Exception as e:
                log(f"[1] erro evaluate: {e}")
                break

            if resultado.get("status") != 200:
                log(f"[1] HTTP {resultado.get('status')}, parando")
                break

            novos, tp = await parse_results_json(resultado["body"])
            total_page = tp or total_page
            log(f"[1] pag {pagina}/{total_page}: +{novos} (total {len(itens)})")
            if novos == 0:
                break
            pagina += 1
    finally:
        await pg.close()

    return itens


# ============ ESTAGIO 2: DETALHES ============
def extrair_detalhes(html, url, capa_listagem=None):
    soup = BeautifulSoup(html, "lxml")
    out = {
        "url": url, "titulo": None, "capa": capa_listagem, "sinopse": None,
        "ano": None, "generos": [], "episodios": [],
    }

    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(" ", strip=True)
        t = re.sub(r"^\s*assistir\s+", "", t, flags=re.I)
        t = re.sub(
            r"\s+(dublado|legendado|online|hd|completo|em\s+hd|"
            r"online\s+em\s+hd|dublado\s+online|legendado\s+online)+\s*$",
            "", t, flags=re.I)
        out["titulo"] = t.strip()
    if not out["titulo"] and soup.title:
        out["titulo"] = re.split(r"\s*[-|–]\s*", soup.title.string or "", 1)[0].strip()

    for sel in [".poster img", ".thumb img", "img.cover", ".capa img"]:
        el = soup.select_one(sel)
        if el:
            src = el.get("src") or el.get("data-src")
            if src:
                out["capa"] = urljoin(url, src)
                break
    if not out["capa"]:
        meta = soup.find("meta", property="og:image")
        if meta:
            out["capa"] = meta.get("content")

    for sel in [".sinopse", ".description", ".wp-content p"]:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(" ", strip=True)
            if len(txt) > 40:
                out["sinopse"] = txt
                break

    m = re.search(r"\b(19[89]\d|20[0-2]\d)\b", soup.get_text())
    if m:
        out["ano"] = int(m.group(0))

    for a in soup.select('a[href*="/genero/"]'):
        g = a.get_text(strip=True)
        if g and g not in out["generos"] and len(g) < 40:
            out["generos"].append(g)

    # eps: /video/a/{id}/
    vistos = set()
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if not re.search(r"/video/a/\d+/?$", h):
            continue
        ep_url = urljoin(url, h)
        if ep_url in vistos:
            continue
        vistos.add(ep_url)
        txt = a.get_text(" ", strip=True)
        m = re.search(r"(\d+)", txt)
        numero = int(m.group(1)) if m else None
        if numero is None:
            m = re.search(r"/video/a/(\d+)/?$", h)
            if m:
                v = int(m.group(1))
                numero = v if v < 1000 else None
        if numero is None:
            continue
        out["episodios"].append({
            "numero": numero,
            "titulo": txt[:80] or f"Ep {numero}",
            "url": ep_url,
        })
    out["episodios"].sort(key=lambda e: e["numero"] or 0)
    return out


# ============ DECISAO POR EP ============
def processar_ep(aid, numero, ep, min_alive=2):
    """Retorna acao: 'novo', 'add_src', 'skip'.

    Politica:
      - ep nao existe                    -> 'novo'
      - ja tem source=FONTE nesse ep     -> 'skip' (nao duplica)
      - tem >= min_alive sources alive   -> 'skip' (redundancia ok)
      - senao                            -> 'add_src'
    """
    ep_row = SB.table("episodes").select("id")\
        .eq("anime_id", aid).eq("numero", numero).limit(1).execute().data or []
    if not ep_row:
        return "novo", None

    eid = ep_row[0]["id"]
    srcs = SB.table("episode_sources").select("id,source,status")\
        .eq("episode_id", eid).execute().data or []

    if any(s.get("source") == FONTE for s in srcs):
        return "skip", eid

    alive = sum(1 for s in srcs if s.get("status") == "alive")
    if alive >= min_alive:
        return "skip", eid

    return "add_src", eid


def gravar_ep(aid, numero, ep, eid, acao):
    """acao = 'novo' ou 'add_src'."""
    embed_url = ep.get("embed_url")
    if not embed_url:
        return

    if acao == "novo":
        SB.table("episodes").upsert({
            "anime_id": aid,
            "numero": numero,
            "titulo": ep.get("titulo") or f"Ep {numero}",
            "status": "unknown",
        }, on_conflict="anime_id,numero").execute()
        r = SB.table("episodes").select("id")\
            .eq("anime_id", aid).eq("numero", numero).single().execute()
        eid = r.data["id"]

    SB.table("episode_sources").upsert({
        "episode_id": eid,
        "source": FONTE,
        "url": ep.get("url"),
        "embed_url": embed_url,
        "embed_type": ep.get("embed_type"),
        "embed_id": embed_token_id(embed_url),
        "status": "unknown",
        "checked_at": iso_now(),
    }, on_conflict="episode_id,source").execute()


# ============ PIPELINE ============
async def pipeline(batch=50, pages_limit=0, workers_detail=4, workers_embed=4,
                   dry_run=False, enriquecer=True, min_alive=2):
    mapa = carregar_mapa_animes()

    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await b.new_context(user_agent=UA, viewport={"width":1920,"height":1080}, locale="pt-BR")

        animes = []
        vistos_slug = set()
        for listagem in LISTAGENS:
            animes_da_listagem = await listar_animes(ctx, listagem, pages_limit=pages_limit)
            novos_unicos = 0
            for a in animes_da_listagem:
                if a["slug_raw"] in vistos_slug:
                    continue
                vistos_slug.add(a["slug_raw"])
                animes.append(a)
                novos_unicos += 1
            log(f"[=] {listagem.split('?')[0].split('/')[-1]}: "
                f"{len(animes_da_listagem)} listados, {novos_unicos} únicos")
        log(f"[=] {len(animes)} animes na fila (total, sem duplicata)")

        resultado = []
        for a in animes:
            resultado.append({
                "url": a["url"],
                "slug_raw": a["slug_raw"],
                "capa": a.get("capa"),
                "slug_limpo": limpar_slug(a["slug_raw"]),
            })

        sem2 = asyncio.Semaphore(workers_detail)
        sem3 = asyncio.Semaphore(workers_embed)
        stats = {"novos": 0, "enriquecidos": 0, "skip": 0, "erro": 0,
                 "ep_novo": 0, "ep_add": 0, "ep_skip": 0, "sem_embed": 0}

        async def detalhe(i, item):
            async with sem2:
                pg = await ctx.new_page()
                try:
                    await pg.goto(item["url"], wait_until="networkidle", timeout=45000)
                    await pg.wait_for_timeout(2500)
                    for _ in range(4):
                        await pg.mouse.wheel(0, 4000)
                        await pg.wait_for_timeout(500)
                    info = extrair_detalhes(await pg.content(), item["url"],
                                            capa_listagem=item.get("capa"))
                    resultado[i].update(info)

                    if not info.get("episodios"):
                        log(f"[2] {i+1}/{len(resultado)} {item['slug_raw']} SEM EPS")
                        stats["skip"] += 1
                        resultado[i]["_aid"] = None
                        return

                    existente, como = achar_anime_existente(
                        mapa, info.get("titulo"), item["slug_raw"])
                    if existente:
                        resultado[i]["_aid"] = existente["id"]
                        resultado[i]["_match"] = como
                        stats["enriquecidos"] += 1
                        log(f"[2] {i+1}/{len(resultado)} {item['slug_raw']} "
                            f"MATCH({como})={existente['slug']} ({len(info['episodios'])} eps)")
                        if not dry_run and enriquecer:
                            anexar_source(existente["id"], FONTE)
                    else:
                        if dry_run:
                            resultado[i]["_aid"] = -1
                            stats["novos"] += 1
                            log(f"[2] {i+1}/{len(resultado)} {item['slug_raw']} NOVO(dry)")
                        else:
                            SB.table("animes").upsert({
                                "slug": item["slug_limpo"],
                                "titulo": info.get("titulo") or item["slug_raw"],
                                "capa": info.get("capa"),
                                "sinopse": info.get("sinopse"),
                                "ano": info.get("ano"),
                                "generos": info.get("generos") or [],
                                "sources": [FONTE],
                                "episodes_count": len(info["episodios"]),
                                "scraped_at": iso_now(),
                            }, on_conflict="slug").execute()
                            r = SB.table("animes").select("id")\
                                .eq("slug", item["slug_limpo"]).single().execute()
                            aid = r.data["id"]
                            resultado[i]["_aid"] = aid
                            novo_reg = {"id": aid, "slug": item["slug_limpo"],
                                        "titulo": info.get("titulo"), "sources": [FONTE]}
                            mapa[0][item["slug_limpo"]] = novo_reg
                            nt = norm_titulo(info.get("titulo") or "")
                            if nt and nt not in mapa[1]:
                                mapa[1][nt] = novo_reg
                            stats["novos"] += 1
                            log(f"[2] {i+1}/{len(resultado)} {item['slug_raw']} NOVO "
                                f"({len(info['episodios'])} eps)")
                except Exception as e:
                    stats["erro"] += 1
                    log(f"[2] {i+1}/{len(resultado)} ERRO: {str(e)[:100]}")
                    resultado[i]["_aid"] = None
                finally:
                    await pg.close()

        async def embed_ep(ai, ei, ep):
            async with sem3:
                aid = resultado[ai].get("_aid")
                if not aid or aid == -1:
                    return
                numero = ep.get("numero")
                if numero is None:
                    return

                acao, eid = processar_ep(aid, numero, ep, min_alive)
                if acao == "skip":
                    stats["ep_skip"] += 1
                    return

                embed, tipo = await capturar_embed(ctx, ep["url"])
                if not embed:
                    stats["sem_embed"] += 1
                    log(f"[3] SEM {resultado[ai]['slug_raw']} ep{numero}")
                    return

                ep["embed_url"] = embed
                ep["embed_type"] = tipo
                stats["ep_novo" if acao == "novo" else "ep_add"] += 1
                log(f"[3] {acao:8} {resultado[ai]['slug_raw']} ep{numero}")

                if not dry_run:
                    try:
                        gravar_ep(aid, numero, ep, eid, acao)
                    except Exception as e:
                        log(f"  [sb erro] {e}")

        async def processar_lote(idx, lote):
            log(f"\n[===] LOTE {idx} ({len(lote)}) ===")
            await asyncio.gather(*(detalhe(i, it) for i, it in lote))
            tarefas = []
            for i, _ in lote:
                for ei, ep in enumerate(resultado[i].get("episodios", [])):
                    tarefas.append(embed_ep(i, ei, ep))
            for k in range(0, len(tarefas), workers_embed * 2):
                await asyncio.gather(*tarefas[k:k + workers_embed * 2])
            log(f"[===] LOTE {idx} pronto | {stats}")

        pares = list(enumerate(resultado))
        tam = batch or len(pares)
        for idx in range((len(pares) + tam - 1) // tam):
            await processar_lote(idx + 1, pares[idx * tam:(idx + 1) * tam])

        await b.close()

    log(f"\n[=] PRONTO | {stats}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--pages-limit", type=int, default=0)
    ap.add_argument("--workers-detail", type=int, default=4)
    ap.add_argument("--workers-embed", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true",
                    help="nao grava nada, so reporta")
    ap.add_argument("--no-enriquecer", action="store_true",
                    help="nao adiciona source ao anime existente (so processa eps)")
    ap.add_argument("--min-alive", type=int, default=2,
                    help="so adiciona nova source se o ep tem menos que N alive (default 2)")
    args = ap.parse_args()
    asyncio.run(pipeline(
        batch=args.batch, pages_limit=args.pages_limit,
        workers_detail=args.workers_detail, workers_embed=args.workers_embed,
        dry_run=args.dry_run, enriquecer=not args.no_enriquecer,
        min_alive=args.min_alive,
    ))


if __name__ == "__main__":
    main()
