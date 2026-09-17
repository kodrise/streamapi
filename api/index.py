# language: Python, file: api/index.py
# API StreamAPI — Vercel serverless
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from supabase import create_client


# ============================================================
# Config
# ============================================================
SB_URL = os.environ.get("SUPABASE_URL")
SB_KEY = os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SB_URL or not SB_KEY:
    raise RuntimeError("Faltam SUPABASE_URL e SUPABASE_ANON_KEY nas variaveis de ambiente")

SB = create_client(SB_URL, SB_KEY)

app = FastAPI(
    title="StreamAPI",
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

CACHE_HEADERS = {
    'Cache-Control': 'public, max-age=300, must-revalidate',
    'CDN-Cache-Control': 'public, s-maxage=600, stale-while-revalidate=3600',
    'Vercel-CDN-Cache-Control': 'public, s-maxage=600, stale-while-revalidate=3600',
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ============================================================
# Helper: aceita slug (string) ou id (int)
# ============================================================
def _buscar_anime(identificador):
    """Retorna dict {id, slug} pelo slug ou pelo id. None se nao achar."""
    ident = str(identificador)
    if ident.isdigit():
        r = SB.table("animes").select("id,slug").eq("id", int(ident)).limit(1).execute()
        if r.data:
            return r.data[0]
    r = SB.table("animes").select("id,slug").eq("slug", ident).limit(1).execute()
    return r.data[0] if r.data else None


# ============================================================
# Health
# ============================================================
@app.get("/health")
@app.get("/api/health")
def health():
    try:
        r = SB.table("animes").select("id", count="exact").limit(1).execute()
        return {"ok": True, "animes": r.count or 0}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ============================================================
# Generos
# ============================================================
@app.get("/generos")
@app.get("/api/generos")
def generos():
    from collections import Counter
    counter = Counter()
    for a in SB.table("animes").select("generos").execute().data or []:
        for g in a.get("generos") or []:
            counter[g] += 1
    return [{"nome": k, "total_animes": v} for k, v in counter.most_common()]


# ============================================================
# Lista de animes (filtros)
# ============================================================
@app.get("/animes")
@app.get("/api/animes")
def listar_animes(
    genero: Optional[str] = None,
    tipo: Optional[str] = None,
    audio: Optional[str] = None,
    ano: Optional[int] = None,
    ano_min: Optional[int] = None,
    ano_max: Optional[int] = None,
    nota_min: Optional[float] = None,
    status: Optional[str] = None,
    source: Optional[str] = None,
    busca: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    ordem: str = Query("id", pattern="^(id|titulo|ano|nota|updated_at|scraped_at)$"),
    desc: bool = True,
):
    q = SB.table("animes").select(
        "id,slug,titulo,ano,nota,tipo,audio,generos,capa,status,sources,episodes_count",
        count="exact").not_.eq("morto", True)

    if genero:   q = q.contains("generos", [genero])
    if tipo:     q = q.eq("tipo", tipo)
    if audio:    q = q.contains("audio", [audio])
    if source:   q = q.contains("sources", [source])
    if ano:      q = q.eq("ano", ano)
    if ano_min:  q = q.gte("ano", ano_min)
    if ano_max:  q = q.lte("ano", ano_max)
    if nota_min: q = q.gte("nota", nota_min)
    if status:   q = q.eq("status", status)
    if busca:    q = q.ilike("titulo", f"%{busca}%")

    ini = (page - 1) * per_page
    q = q.order(ordem, desc=desc).range(ini, ini + per_page - 1)
    r = q.execute()
    return {"total": r.count or 0, "page": page, "per_page": per_page, "animes": r.data or []}


# ============================================================
# Detalhe do anime (por slug ou id) com eps
# ============================================================
@app.get("/animes/{identificador}")
@app.get("/api/animes/{identificador}")
def detalhe(identificador: str):
    found = _buscar_anime(identificador)
    if not found:
        raise HTTPException(404, "anime nao encontrado")
    check = SB.table("animes").select("morto").eq("id", found["id"]).single().execute()
    if check.data and check.data.get("morto"):
        raise HTTPException(404, "anime indisponivel")

    anime = SB.table("animes").select(
        "id,slug,titulo,titulo_original,capa,sinopse,ano,nota,status,"
        "generos,sources,episodes_count,tipo,audio,scraped_at,created_at,updated_at"
    ).eq("id", found["id"]).single().execute().data

    eps = SB.table("episodes").select(
        "id,numero,titulo,episode_name,thumb,audio,status")\
          .eq("anime_id", anime["id"]).order("numero").execute().data or []

    ids = [e["id"] for e in eps]
    srcs_map = {}
    if ids:
        for s in SB.table("episode_sources").select(
                "episode_id,source,embed_url,status").in_("episode_id", ids).execute().data or []:
            srcs_map.setdefault(s["episode_id"], []).append({
                "source": s["source"],
                "status": s.get("status"),
                "has_stream": bool(s.get("embed_url")),
            })
    for e in eps:
        e["sources"] = srcs_map.get(e["id"], [])

    anime["episodios"] = eps
    return JSONResponse(content=anime, headers=CACHE_HEADERS)


# ============================================================
# Só os eps
# ============================================================
@app.get("/animes/{identificador}/episodes")
@app.get("/api/animes/{identificador}/episodes")
def episodios(identificador: str):
    found = _buscar_anime(identificador)
    if not found:
        raise HTTPException(404, "anime nao encontrado")

    eps = SB.table("episodes").select(
        "id,numero,titulo,episode_name,thumb,audio,status")\
          .eq("anime_id", found["id"]).order("numero").execute().data or []

    ids = [e["id"] for e in eps]
    srcs_map = {}
    if ids:
        for s in SB.table("episode_sources").select(
                "episode_id,source,embed_url,status").in_("episode_id", ids).execute().data or []:
            srcs_map.setdefault(s["episode_id"], []).append({
                "source": s["source"],
                "status": s.get("status"),
                "has_stream": bool(s.get("embed_url")),
            })
    for e in eps:
        e["sources"] = srcs_map.get(e["id"], [])

    return {"anime": found["slug"], "id": found["id"], "count": len(eps), "episodes": eps}




# ============================================================
# Lite — payload mínimo pro player (numero + status, sem sources)
# ============================================================
@app.get("/animes/{identificador}/lite")
@app.get("/api/animes/{identificador}/lite")
def anime_lite(identificador: str):
    found = _buscar_anime(identificador)
    if not found:
        raise HTTPException(404, "anime nao encontrado")
    # checa se o anime ta marcado como morto
    check = SB.table("animes").select("morto").eq("id", found["id"]).single().execute()
    if check.data and check.data.get("morto"):
        raise HTTPException(404, "anime indisponivel")

    anime = SB.table("animes").select("id,slug,titulo")\
             .eq("id", found["id"]).single().execute().data

    eps = SB.table("episodes").select("numero,episode_name,thumb,audio,status")\
           .eq("anime_id", anime["id"]).order("numero").execute().data or []

    return JSONResponse(
        content={
            "id": anime["id"],
            "slug": anime["slug"],
            "titulo": anime["titulo"],
            "episodios": [{
                "numero": e["numero"],
                "titulo": e.get("episode_name") or f"Ep {e['numero']}",
                "thumb": e.get("thumb"),
                "audio": e.get("audio"),
                "status": e.get("status") or "unknown",
            } for e in eps],
        },
        headers=CACHE_HEADERS,
    )


# ============================================================
# Resolve — só metadados (nunca expõe embed_url)
# ============================================================
@app.get("/resolve/{identificador}/{numero}")
@app.get("/api/resolve/{identificador}/{numero}")
def resolver(identificador: str, numero: int, source: Optional[str] = None):
    found = _buscar_anime(identificador)
    if not found:
        raise HTTPException(404, "anime nao encontrado")

    ep = SB.table("episodes").select("id")\
         .eq("anime_id", found["id"]).eq("numero", numero).limit(1).execute()
    if not ep.data:
        raise HTTPException(404, "episodio nao encontrado")

    eid = ep.data[0]["id"]
    rows = SB.table("episode_sources").select("source,embed_url,status")\
           .eq("episode_id", eid).execute().data or []
    if not rows:
        raise HTTPException(404, "sem fonte")

    if source:
        sel = [r for r in rows if r["source"] == source]
        if sel:
            rows = sel

    rows.sort(key=lambda r: 0 if r.get("status") == "alive" else 1)
    limpas = [{
        "source": r["source"],
        "status": r.get("status"),
        "has_stream": bool(r.get("embed_url")),
    } for r in rows]

    return {
        "slug": found["slug"],
        "id": found["id"],
        "numero": numero,
        "stream_url": f"/api/stream/{found['slug']}/{numero}",
        "sources": limpas,
    }


# ============================================================
# Stream — 302 pro embed real
# ============================================================
@app.get("/stream/{identificador}/{numero}")
@app.get("/api/stream/{identificador}/{numero}")
def stream(identificador: str, numero: int, source: Optional[str] = None):
    found = _buscar_anime(identificador)
    if not found:
        raise HTTPException(404, "anime nao encontrado")

    ep = SB.table("episodes").select("id")\
         .eq("anime_id", found["id"]).eq("numero", numero).limit(1).execute()
    if not ep.data:
        raise HTTPException(404, "episodio nao encontrado")

    rows = SB.table("episode_sources").select("source,embed_url,status")\
           .eq("episode_id", ep.data[0]["id"]).execute().data or []
    if source:
        rows = [r for r in rows if r["source"] == source] or rows
    rows = [r for r in rows if r.get("embed_url")]
    if not rows:
        raise HTTPException(404, "sem fonte")

    rows.sort(key=lambda r: 0 if r.get("status") == "alive" else 1)
    return RedirectResponse(rows[0]["embed_url"], status_code=302)

# ============================================================
# Embed — devolve o embed_url cru pro iframe usar direto
# ============================================================
@app.get("/embed/{identificador}/{numero}")
@app.get("/api/embed/{identificador}/{numero}")
def embed_raw(identificador: str, numero: int):
    """Retorna o embed_url do Blogger pro iframe apontar direto."""
    found = _buscar_anime(identificador)
    if not found:
        raise HTTPException(404, "anime nao encontrado")

    ep = SB.table("episodes").select("id")\
         .eq("anime_id", found["id"]).eq("numero", numero).limit(1).execute()
    if not ep.data:
        raise HTTPException(404, "episodio nao encontrado")

    rows = SB.table("episode_sources").select("source,embed_url,status")\
           .eq("episode_id", ep.data[0]["id"]).execute().data or []
    rows = [r for r in rows if r.get("embed_url")]
    if not rows:
        raise HTTPException(404, "sem embed")
    rows.sort(key=lambda r: 0 if r.get("status") == "alive" else 1)

    return JSONResponse(
        content={
            "slug": found["slug"],
            "numero": numero,
            "embed_url": rows[0]["embed_url"],
            "source": rows[0]["source"],
            "status": rows[0].get("status"),
        },
        headers=CACHE_HEADERS,
    )
