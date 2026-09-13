import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from supabase import create_client

SB_URL = os.environ.get("SUPABASE_URL")
SB_KEY = os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SB_URL or not SB_KEY:
    raise RuntimeError(
        "Faltam SUPABASE_URL e SUPABASE_ANON_KEY nas variaveis de ambiente"
    )

SB = create_client(SB_URL, SB_KEY)

app = FastAPI(
    title="StreamAPI",
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

@app.get("/health")
@app.get("/api/health")
def health():
    try:
        result = (
            SB.table("animes")
            .select("id", count="exact")
            .limit(1)
            .execute()
        )
        return {"ok": True, "animes": result.count or 0}
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": str(e)},
            status_code=500,
        )

@app.get("/generos")
@app.get("/api/generos")
def generos():
    try:
        result = SB.table("generos").select("*").execute()
        return result.data or []
    except Exception:
        from collections import Counter

        counter = Counter()
        result = SB.table("animes").select("generos").execute()

        for anime in result.data or []:
            for genero in anime.get("generos") or []:
                counter[genero] += 1

        return [
            {"nome": nome, "total_animes": total}
            for nome, total in counter.most_common()
        ]

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
    ordem: str = Query(
        "id",
        pattern="^(id|titulo|ano|nota|updated_at|scraped_at)$",
    ),
    desc: bool = True,
):
    query = (
        SB.table("animes")
        .select(
            """
            id,
            slug,
            titulo,
            ano,
            nota,
            tipo,
            audio,
            generos,
            capa,
            status,
            sources,
            episodes_count
            """,
            count="exact",
        )
    )

    if genero:
        query = query.contains("generos", [genero])

    if tipo:
        query = query.eq("tipo", tipo)

    if audio:
        query = query.contains("audio", [audio])

    if source:
        query = query.contains("sources", [source])

    if ano:
        query = query.eq("ano", ano)

    if ano_min:
        query = query.gte("ano", ano_min)

    if ano_max:
        query = query.lte("ano", ano_max)

    if nota_min:
        query = query.gte("nota", nota_min)

    if status:
        query = query.eq("status", status)

    if busca:
        query = query.ilike("titulo", f"%{busca}%")

    inicio = (page - 1) * per_page
    fim = inicio + per_page - 1

    query = query.order(ordem, desc=desc).range(inicio, fim)

    result = query.execute()

    return {
        "total": result.count or 0,
        "page": page,
        "per_page": per_page,
        "animes": result.data or [],
    }

def _get_anime(slug: str):
    try:
        result = (
            SB.table("animes")
            .select("id,slug,titulo,titulo_original,capa,sinopse,ano,nota,status,generos,sources,episodes_count,tipo,audio,scraped_at,created_at,updated_at")
            .eq("slug", slug)
            .single()
            .execute()
        )
        return result.data
    except Exception:
        return None

def _get_episodes(anime_id: int):
    episodes = (
        SB.table("episodes")
        .select("id,numero,titulo,status")
        .eq("anime_id", anime_id)
        .order("numero")
        .execute()
        .data
        or []
    )

    episode_ids = [ep["id"] for ep in episodes]
    sources_by_episode = {}

    if episode_ids:
        sources = (
            SB.table("episode_sources")
            .select(
                "episode_id,source,embed_url,embed_id,embed_type,status,url"
            )
            .in_("episode_id", episode_ids)
            .execute()
            .data
            or []
        )

        for source in sources:
            sources_by_episode.setdefault(
                source["episode_id"], []
            ).append(
                {
                    "source": source["source"],
                    "embed_url": source["embed_url"],
                    "embed_id": source["embed_id"],
                    "embed_type": source["embed_type"],
                    "status": source["status"],
                    "url": source["url"],
                }
            )

    for episode in episodes:
        episode["sources"] = sources_by_episode.get(
            episode["id"], []
        )

    return episodes

@app.get("/animes/{slug}")
@app.get("/api/animes/{slug}")
def detalhe(slug: str):
    anime = _get_anime(slug)

    if not anime:
        raise HTTPException(
            status_code=404,
            detail="anime nao encontrado",
        )

    anime["episodios"] = _get_episodes(anime["id"])

    return anime

@app.get("/animes/{slug}/episodes")
@app.get("/api/animes/{slug}/episodes")
def episodios(slug: str):
    anime = _get_anime(slug)

    if not anime:
        raise HTTPException(
            status_code=404,
            detail="anime nao encontrado",
        )

    episodes = _get_episodes(anime["id"])

    return {
        "anime": slug,
        "count": len(episodes),
        "episodes": episodes,
    }

@app.get("/resolve/{slug}/{numero}")
@app.get("/api/resolve/{slug}/{numero}")
def resolver(
    slug: str,
    numero: int,
    source: Optional[str] = None,
):
    anime = _get_anime(slug)

    if not anime:
        raise HTTPException(
            status_code=404,
            detail="anime nao encontrado",
        )

    episode = (
        SB.table("episodes")
        .select("id")
        .eq("anime_id", anime["id"])
        .eq("numero", numero)
        .limit(1)
        .execute()
    )

    if not episode.data:
        raise HTTPException(
            status_code=404,
            detail="episodio nao encontrado",
        )

    episode_id = episode.data[0]["id"]

    sources = (
        SB.table("episode_sources")
        .select(
            "source,embed_url,embed_id,embed_type,status,url"
        )
        .eq("episode_id", episode_id)
        .execute()
        .data
        or []
    )

    if not sources:
        raise HTTPException(
            status_code=404,
            detail="sem fonte",
        )

    if source:
        selected = [
            item for item in sources
            if item["source"] == source
        ]

        if selected:
            sources = selected

    sources.sort(
        key=lambda item: (
            0 if item.get("status") == "alive" else 1
        )
    )

    return {
        "slug": slug,
        "numero": numero,
        "sources": sources,
    }

@app.get("/stream/{slug}/{numero}")
@app.get("/api/stream/{slug}/{numero}")
def stream(
    slug: str,
    numero: int,
    source: Optional[str] = None,
):
    result = resolver(slug, numero, source)

    url = result["sources"][0].get("embed_url")

    if not url:
        raise HTTPException(
            status_code=422,
            detail="embed_url vazio",
        )

    return RedirectResponse(url, status_code=302)
