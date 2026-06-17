# Runbook

Fill in as built; keep accurate, not aspirational.

## Environment
```
cp .env.example .env     # DB_URL, ANTHROPIC_API_KEY, EDGAR_IDENTITY="Name <email>"
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Database
```
createdb peerdesk
psql peerdesk -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql peerdesk -f db/schema.sql
python -m scripts.load_seed         # loads PEB + 2 peers, sourced figures
```

## Tests
```
pytest                              # every tool tested against seed DB
```

## Service
```powershell
# Terminal 1 — FastAPI backend (port 8000)
uvicorn api.main:app --reload --reload-dir api --reload-dir agent --reload-dir tools --port 8000

# Terminal 2 — Vue frontend (port 5173)
cd frontend
npm run dev
```
Open http://localhost:5173 in the browser.

## Ingestion (live)
```
python -m scripts.ingest_xbrl       # GAAP statements → raw_facts → statements
python -m scripts.extract_kpis      # Claude pass over 8-K exhibits → kpis
python -m scripts.index_documents   # chunk + embed → doc_chunks
```
