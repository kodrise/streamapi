#!/usr/bin/env python3
"""Casa títulos do TioAnime com o catálogo do Supabase usando Dahl (LLM)."""
import os, re, json, time, argparse
from difflib import SequenceMatcher
from curl_cffi import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
SB = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

DAHL_KEY = os.environ.get("DAHL_KEY") or "dahl_PtKgnBRzCHqgzbwXxb82LByne6SUm5dTc"
DAHL_URL = "https://inference.dahl.global/v1/chat/completions"
MODEL = "MiniMaxAI/MiniMax-M2.7"  # ou "THUDM/GLM-5.3-Flash" se disponível


def strip_think(text):
    """Remove <think>...</think> do output."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def carregar_catalogo():
    itens = []
    offset = 0
    while True:
        r = SB.table("animes").select("id,slug,titulo").range(offset, offset + 999).execute().data or []
        if not r:
            break
        itens.extend(r)
        if len(r) < 1000:
            break
        offset += 1000
    return itens


def candidatos(titulo_tio, catalogo, top=10):
    """Retorna os N títulos do catálogo mais próximos por similaridade.

    Penaliza candidatos com sufixos de temporada/dublado quando o título
    do TioAnime não tem número nem indicação de dublado/legendado.
    """
    t = titulo_tio.lower()
    tem_numero_tio = bool(re.search(r"\b(?:season\s*)?\d+\b|\b(?:ii|iii|iv)\b", t))
    tem_dublado_tio = bool(re.search(r"dublado|dublada|dub\b", t))

    scored = []
    for a in catalogo:
        ct = (a.get("titulo") or "").lower()
        cs = (a.get("slug") or "").lower()
        score = SequenceMatcher(None, t, ct).ratio()
        tokens_t = set(re.findall(r"\w+", t))
        tokens_c = set(re.findall(r"\w+", ct))
        if tokens_t and tokens_t & tokens_c:
            score += 0.3

        # penaliza sufixos quando o título do TioAnime não tem
        if not tem_numero_tio and re.search(r"-(?:season-)?\d+\b|\b(?:2|3|4|5)(?:ª|a)?\s*temporada", cs):
            score -= 0.25
        if not tem_numero_tio and re.search(r"final-season|\bthe-final", cs):
            score -= 0.15
        if not tem_dublado_tio and re.search(r"-dublado", cs):
            score -= 0.10
        if re.search(r"-legendado", cs):
            score -= 0.05

        # bónus para slugs limpos (sem sufixos)
        if not re.search(r"-(?:\d+|dublado|legendado|online|hd|temporada)", cs):
            score += 0.15

        scored.append((score, a))
    scored.sort(key=lambda x: -x[0])
    return [a for _, a in scored[:top]]


def perguntar_llm(titulo_tio, candidatos):
    """Pergunta ao LLM qual candidato é o mesmo anime."""
    lista = "\n".join(f"{i+1}. {c['titulo']} (slug: {c['slug']})" for i, c in enumerate(candidatos))
    tem_numero = bool(re.search(r"\b(?:season\s*)?\d+\b", titulo_tio))
    prompt = (
        f"Preciso casar um anime entre dois sites.\n\n"
        f"Título no TioAnime: \"{titulo_tio}\"\n\n"
        f"Candidatos no catálogo:\n{lista}\n\n"
        f"REGRAS CRÍTICAS:\n"
        f"- O título do TioAnime {'TEM' if tem_numero else 'NÃO tem'} número de temporada.\n"
        + (
            "- Casa APENAS com candidato que tenha o MESMO número no slug.\n"
            if tem_numero else
            "- Casa com candidato SEM número de temporada no slug. "
            "NUNCA escolher slug com -2, -3, -season-2 ou *temporada 2/3*.\n"
        ) +
        f"- Se o anime tem versão dublada, o slug pode ter -dublado. Se o título "
        f"do TioAnime não diz 'dublado', prefere o slug sem -dublado.\n"
        f"- Exemplos corretos:\n"
        f"  - 'Shingeki no Kyojin' → slug 'shingeki-no-kyojin' (NÃO '-2')\n"
        f"  - 'Shingeki no Kyojin 2' → slug 'shingeki-no-kyojin-2'\n"
        f"  - 'Solo Leveling' → 'solo-leveling' (NÃO 'solo-leveling-2')\n\n"
        f"Responde APENAS com o número (1-{len(candidatos)}) ou 0 se nenhum for igual."
    )
    try:
        r = requests.post(DAHL_URL, headers={
            "Authorization": f"Bearer {DAHL_KEY}",
            "Content-Type": "application/json",
        }, json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 200,
        }, impersonate="chrome131", timeout=60)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:150]}"
        data = r.json()
        raw = data["choices"][0]["message"]["content"]
        clean = strip_think(raw)
        # extrai o primeiro número
        m = re.search(r"\b(\d+)\b", clean)
        if not m:
            return None, f"resposta inválida: {clean[:100]!r}"
        n = int(m.group(1))
        if n == 0:
            return None, "sem match"
        if 1 <= n <= len(candidatos):
            return candidatos[n - 1], "ok"
        return None, f"número fora de range: {n}"
    except Exception as e:
        return None, f"erro: {str(e)[:100]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--titulos", required=True, help="Ficheiro com títulos do TioAnime (1 por linha)")
    ap.add_argument("--out", default="/tmp/matches.json")
    ap.add_argument("--top", type=int, default=10, help="Nº de candidatos a enviar ao LLM")
    ap.add_argument("--delay", type=float, default=0.5, help="Segundos entre chamadas")
    args = ap.parse_args()

    print(f"[*] carregando catálogo...")
    catalogo = carregar_catalogo()
    print(f"    {len(catalogo)} animes no Supabase")

    with open(args.titulos) as f:
        titulos_tio = [l.strip() for l in f if l.strip()]
    print(f"[*] {len(titulos_tio)} títulos do TioAnime para casar\n")

    matches = {}
    sem_match = []
    erros = []

    for i, t in enumerate(titulos_tio, 1):
        cands = candidatos(t, catalogo, top=args.top)
        if not cands:
            continue
        melhor, motivo = perguntar_llm(t, cands)
        if melhor:
            matches[t] = {"slug": melhor["slug"], "titulo_db": melhor["titulo"], "id": melhor["id"]}
            print(f"[{i:3}/{len(titulos_tio)}] ✅ {t!r} → {melhor['slug']}")
        else:
            if motivo == "sem match":
                sem_match.append(t)
                print(f"[{i:3}/{len(titulos_tio)}] ⚪ {t!r} → {motivo}")
            else:
                erros.append((t, motivo))
                print(f"[{i:3}/{len(titulos_tio)}] ❌ {t!r} → {motivo}")
        time.sleep(args.delay)

    with open(args.out, "w") as f:
        json.dump({"matches": matches, "sem_match": sem_match, "erros": erros}, f, ensure_ascii=False, indent=2)

    print(f"\n[=] PRONTO")
    print(f"    matches: {len(matches)}")
    print(f"    sem match: {len(sem_match)}")
    print(f"    erros: {len(erros)}")
    print(f"    salvo em: {args.out}")


if __name__ == "__main__":
    main()
