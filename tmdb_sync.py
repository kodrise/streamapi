#!/usr/bin/env python3
# language: Python, file: tmdb_sync.py
# Sincroniza filmes/séries do TMDB → Supabase. Zero scraping, zero Cloudflare.
import os, argparse, time
from curl_cffi import requests
from dotenv import load_dotenv

# --- monkey-patch httpx para HTTP/1.1 (evita stream limit HTTP/2 no Supabase) ---
import httpx
_orig_client_init = httpx.Client.__init__
def _http1_init(self, *args, **kwargs):
    kwargs["http2"] = False
    _orig_client_init(self, *args, **kwargs)
httpx.Client.__init__ = _http1_init
# ---------------------------------------------------------------------------

from supabase import create_client

load_dotenv()
SB = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

TMDB_TOKEN = os.environ.get("TMDB_TOKEN", "")

if not TMDB_TOKEN:
    raise RuntimeError(
        "TMDB_TOKEN nao definido. Adiciona ao .env local ou aos secrets do GitHub."
    )
BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"
IMG_ORIG = "https://image.tmdb.org/t/p/w780"
H = {"Authorization": f"Bearer {TMDB_TOKEN}", "Accept": "application/json"}

# Múltiplas listagens por tipo (cobre populares + clássicos + actual + PT-BR)
LISTAGENS = {
    "tv": [
        ("/discover/tv", {"sort_by": "popularity.desc", "without_genres": "10767,10763,10764"}),
        ("/tv/top_rated", {}),
        ("/tv/on_the_air", {}),
        ("/discover/tv", {"with_original_language": "pt", "sort_by": "popularity.desc"}),
    ],
    "movie": [
        ("/movie/popular", {}),
        ("/movie/top_rated", {}),
        ("/movie/now_playing", {}),
        ("/movie/upcoming", {}),
        ("/discover/movie", {"with_original_language": "pt", "sort_by": "popularity.desc"}),
    ],
}


def tmdb(path, params=None, tentativas=5):
    """GET no TMDB com retry agressivo. Connection: close evita o
    RemoteProtocolError por HTTP/2 stream limit."""
    for t in range(tentativas):
        try:
            headers_local = {**H, "Connection": "close"}
            r = requests.get(
                BASE + path,
                headers=headers_local,
                params=params or {},
                impersonate="chrome131",
                timeout=30,
            )
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 * (t + 1))
                continue
            print(f"    [!] tmdb HTTP {r.status_code} em {path}")
            return None
        except Exception as e:
            err = str(e)
            if "RemoteProtocol" in err or "ConnectionTerminated" in err:
                time.sleep(3 * (t + 1))
            else:
                time.sleep(1 * (t + 1))
            if t == tentativas - 1:
                print(f"    [!] tmdb falhou ({tentativas}x): {err[:80]}")
                return None
    return None


def montar_embed(tmdb_id, tipo, temporada=None, episodio=None):
    """Constrói URL do vidlink.pro (iframe, não expira)."""
    if tipo == "filme":
        return f"https://vidlink.pro/movie/{tmdb_id}"
    return f"https://vidlink.pro/tv/{tmdb_id}/{temporada}/{episodio}"


def gravar_midia(item, tipo):
    titulo = item.get("name") or item.get("title") or "?"
    titulo_orig = item.get("original_name") or item.get("original_title")
    data = item.get("first_air_date") or item.get("release_date") or ""
    ano = int(data[:4]) if data[:4].isdigit() else None

    row = {
        "tmdb_id": item["id"],
        "tipo": tipo,
        "titulo": titulo,
        "titulo_original": titulo_orig,
        "sinopse": item.get("overview") or None,
        "capa": f"{IMG}{item['poster_path']}" if item.get("poster_path") else None,
        "backdrop": f"{IMG_ORIG}{item['backdrop_path']}" if item.get("backdrop_path") else None,
        "ano": ano,
        "nota": round(item.get("vote_average", 0), 1) or None,
        "generos": [],
        "status": None,
        "provider": "vidlink",
    }

    SB.table("midias").upsert(row, on_conflict="tmdb_id,tipo").execute()
    r = SB.table("midias").select("id").eq("tmdb_id", item["id"]).eq("tipo", tipo).single().execute()
    mid = r.data["id"] if r.data else None

    return mid


def detalhes_serie(tmdb_id):
    """Puxa detalhes + temporadas + eps."""
    d = tmdb(tmdb_id_path(tmdb_id), {"language": "pt-BR"})
    if not d:
        return None
    return d


def tmdb_id_path(tid):
    return f"/tv/{tid}"


def sincronizar(tipo="tv", pages=20, max_items=0):
    """Percorre todas as listagens do tipo, grava na tabela midias, e
    (para séries) processa eps após a listagem completa."""
    tipo_norm = "serie" if tipo == "tv" else "filme"
    listagens = LISTAGENS.get(tipo, [])
    total_geral = 0

    for endpoint, params_base in listagens:
        print(f"\n[*] {endpoint} {params_base.get('with_original_language') or params_base.get('sort_by') or ''}")
        for page in range(1, pages + 1):
            params = {"language": "pt-BR", "page": page, **params_base}
            d = tmdb(endpoint, params)
            if not d:
                break
            results = d.get("results", [])
            if not results:
                break
            for item in results:
                mid = gravar_midia(item, tipo_norm)
                if mid:
                    total_geral += 1
            if page % 5 == 0:
                print(f"    pag {page}: total acumulado {total_geral}")
            time.sleep(0.1)

    print(f"\n[*] total {tipo_norm}: {total_geral}")

    # séries: puxar eps após todas as listagens
    if tipo == "tv":
        print(f"\n[*] puxando detalhes e eps das séries...")
        rows = SB.table("midias").select("id,tmdb_id,titulo").eq("tipo", "serie").execute().data or []
        if max_items:
            rows = rows[:max_items]
        for r in rows:
            detalhes = tmdb(f"/tv/{r['tmdb_id']}", {"language": "pt-BR"})
            if not detalhes:
                continue
            n_eps = detalhes.get("number_of_episodes", 0)
            max_eps_season = max((s.get("episode_count", 0) for s in detalhes.get("seasons", [])), default=0)
            if max_eps_season > 100:
                print(f"    skip {r['titulo'][:45]:45} (temporada com {max_eps_season} eps)")
                continue
            SB.table("midias").update({
                "total_temporadas": detalhes.get("number_of_seasons", 0),
                "total_episodios": n_eps,
                "status": detalhes.get("status"),
                "generos": [g["name"] for g in detalhes.get("genres", [])],
            }).eq("id", r["id"]).execute()

            for s in detalhes.get("seasons", []):
                snum = s.get("season_number", 0)
                if snum == 0:
                    continue
                eps_d = tmdb(f"/tv/{r['tmdb_id']}/season/{snum}", {"language": "pt-BR"})
                if not eps_d:
                    continue
                for ep in eps_d.get("episodes", []):
                    enum = ep.get("episode_number")
                    embed_url = montar_embed(r["tmdb_id"], "serie", snum, enum)
                    res = SB.table("midias_episodios").upsert({
                        "midia_id": r["id"],
                        "temporada": snum,
                        "episodio": enum,
                        "titulo": ep.get("name"),
                        "sinopse": ep.get("overview"),
                        "thumb": f"{IMG}{ep['still_path']}" if ep.get("still_path") else None,
                        "duracao": ep.get("runtime"),
                        "embed_url": embed_url,
                        "provider": "vidlink",
                    }, on_conflict="midia_id,temporada,episodio").execute()
                    eid = res.data[0]["id"] if res.data else None
                    if eid:
                        SB.table("midias_sources").upsert({
                            "episodio_id": eid,
                            "midia_id": r["id"],
                            "source": "vidlink",
                            "embed_url": embed_url,
                            "embed_type": "iframe",
                            "status": "unknown",
                        }, on_conflict="episodio_id,source").execute()
                print(f"    {r['titulo'][:40]:40} S{snum} ({len(eps_d.get('episodes', []))} eps)")
            time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tipo", choices=["tv", "movie", "ambos"], default="ambos")
    ap.add_argument("--pages", type=int, default=20, help="páginas por listagem")
    ap.add_argument("--max", type=int, default=0, help="máx itens por tipo (0 = sem limite)")
    args = ap.parse_args()

    if args.tipo in ("tv", "ambos"):
        sincronizar("tv", args.pages, max_items=args.max)
    if args.tipo in ("movie", "ambos"):
        sincronizar("movie", args.pages, max_items=args.max)

    print(f"\n[=] PRONTO")


if __name__ == "__main__":
    main()
