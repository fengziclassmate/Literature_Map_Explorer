# Literature Map Explorer

Academic literature network analysis tool. The first backend milestone accepts a seed DOI, resolves paper metadata, crawls references and citing papers, stores the result in SQLite, and exposes graph-ready data for a React + Vite + Cytoscape.js frontend.

## Stack

- Backend: Python, FastAPI
- Database: SQLite first; schema is migration-friendly for later PostgreSQL
- Graph analysis: NetworkX
- External APIs: OpenAlex, Semantic Scholar, Crossref
- Frontend target: React, Vite, Cytoscape.js
- Export targets: CSV, BibTeX, GraphML, Markdown

## Project layout

```text
backend/
  app/
    api/
      graph.py
      projects.py
    core/
      settings.py
    db/
      database.py
      schema.sql
    models/
      citation.py
      paper.py
    services/
      crawler.py
      crossref_client.py
      graph_analyzer.py
      graph_builder.py
      http_client.py
      openalex_client.py
      paper_resolver.py
      semantic_scholar_client.py
    main.py
requirements.txt
```

## Run locally

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -r requirements.txt
cd backend
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs`.

## Run the Chinese Web UI

Start the backend first:

```bash
cd backend
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

In a second terminal, start the frontend:

```bash
cd frontend
pnpm install
pnpm dev -- --port 5173
```

Open `http://127.0.0.1:5173` for the Chinese literature-map workspace. The backend API docs remain available at `http://127.0.0.1:8000/docs`.

## Configuration

Environment variables:

- `LME_DATABASE_URL`: defaults to `sqlite:///./literature_map.db`
- `LME_USER_AGENT`: user agent sent to external APIs
- `LME_CONTACT_EMAIL`: optional OpenAlex mailto parameter
- `LME_UNPAYWALL_EMAIL`: email used for Unpaywall open-access PDF lookup
- `LME_PDF_DOWNLOAD_DIR`: local directory for saved open-access PDFs, defaults to `./downloads/pdfs`
- `LME_INSTITUTION_NAME`: optional institution label shown in the PDF access panel
- `LME_LIBRARY_RESOLVER_URL`: optional library link resolver. Supports `{doi}` or appends `?doi=...`
- `LME_EZPROXY_URL_PREFIX`: optional EZProxy prefix. Supports `{url}` or appends the encoded publisher URL
- `LME_CARSI_LOGIN_URL`: optional CARSI login URL shown as an authorized-access handoff
- `LME_WEBVPN_URL`: optional WebVPN login URL shown as an authorized-access handoff
- `LME_INSTITUTION_LOGIN_URL`: optional browser-based institution login or library portal URL
- `SEMANTIC_SCHOLAR_API_KEY`: optional Semantic Scholar API key
- `LME_API_MAX_RETRIES`: defaults to `3`
- `LME_API_CACHE_TTL_SECONDS`: defaults to one day
- `LME_OPENALEX_RPS`, `LME_SEMANTIC_SCHOLAR_RPS`, `LME_CROSSREF_RPS`: per-service rate limits
- `LME_CORS_ORIGINS`: comma-separated frontend origins

## Core behavior

- Current seed input: DOI only.
- Deduplication priority: DOI, then OpenAlex ID, then Semantic Scholar ID, then normalized title.
- Citation edge direction: `source cites target`.
- Automatic PDF fetching is open-access only. It uses existing metadata PDF URLs plus OpenAlex OA, Semantic Scholar OA, and Unpaywall.
- The Paper Card can also show authorized access handoff links for DOI, publisher pages, library resolvers, EZProxy, CARSI, WebVPN, and institution login portals. These links require the user to authenticate normally.
- Users can upload PDFs they have already obtained through authorized access. Uploaded files are stored locally in `LME_PDF_DOWNLOAD_DIR` and linked to the corresponding Paper Card.
- The app intentionally does not enable Sci-Hub, LibGen, Tor, credential automation, or any mechanism that bypasses publisher or institution access controls.
- Crawl controls:
  - `max_depth_backward`: reference expansion depth
  - `max_depth_forward`: citing-paper expansion depth
  - `max_papers_total`: hard cap on stored graph nodes

## API examples

Create a project from a DOI:

```bash
curl -X POST http://127.0.0.1:8000/projects \
  -H "Content-Type: application/json" \
  -d '{
    "seed_doi": "10.1038/nature14539",
    "max_depth_backward": 1,
    "max_depth_forward": 1,
    "max_papers_total": 100
  }'
```

Fetch a Cytoscape-compatible graph:

```bash
curl http://127.0.0.1:8000/graph/<project_id>
```

Analyze graph structure:

```bash
curl http://127.0.0.1:8000/graph/<project_id>/analysis
```

Fetch open-access PDF status or trigger a safe PDF download for a paper:

```bash
curl http://127.0.0.1:8000/papers/<paper_id>/pdf
curl -X POST http://127.0.0.1:8000/papers/<paper_id>/pdf/download
```

Get lawful access links or upload a locally authorized PDF:

```bash
curl http://127.0.0.1:8000/papers/<paper_id>/access-links
curl -X POST http://127.0.0.1:8000/papers/<paper_id>/pdf/upload \
  -H "Content-Type: application/pdf" \
  --data-binary @paper.pdf
```

Export:

```bash
curl "http://127.0.0.1:8000/graph/<project_id>/export?fmt=graphml"
curl "http://127.0.0.1:8000/graph/<project_id>/export?fmt=csv"
curl "http://127.0.0.1:8000/graph/<project_id>/export?fmt=bibtex"
curl "http://127.0.0.1:8000/graph/<project_id>/export?fmt=markdown"
```

## Notes

The API clients share `CachingHttpClient`, which provides error handling, retry with backoff, per-service rate limiting, and SQLite response caching. The synchronous crawl endpoint is intentionally simple for the first milestone; for larger graphs, move crawling into a background worker and keep the service interfaces unchanged.
