# StreamAPI

Scraper + API para sites DooPlay (Goyabu e similares). Catalogo incremental no Firestore, UI web, resolucao de tokens do Blogger em MP4.

## Componentes

- scrape.py — varre a listagem, extrai metadados + eps, empurra pro Firestore em lotes
- api_firestore.py — FastAPI que serve UI + resolve tokens em MP4 fresco
- static/ — frontend

## Setup

pip install -r requirements.txt
playwright install chromium

## Uso

python scrape.py "https://goyabu.io/lista-de-animes" -o catalogo.json --firebase-key serviceAccountKey.json
python api_firestore.py 8000

UI em http://localhost:8000
