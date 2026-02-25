# NetFile Campaign Finance Tracker

## Project Overview
Shasta County campaign finance disclosure tracker. Spoke app in the Atlas hub-and-spoke ecosystem.

- **Codename:** netfile-tracker
- **Port:** 8855
- **Stack:** FastAPI + async SQLAlchemy + aiosqlite + Jinja2/HTMX
- **Data source:** NetFile Connect2 public API (agency ID: CSHA)

## Architecture
- Atlas spoke (hub at :8888)
- Sibling spokes: civic_media (:8000), Shasta-DB (:8844), article-tracker (:5000), Facebook-Offline (:8147), Shasta-PRA-Backup (:8845), Facebook-Monitor (:8150)
- Person model is schema-compatible across all spokes for Atlas People search

**Cross-spoke rules:**
- This app must remain independently functional without Atlas or any other spoke.
- No direct spoke-to-spoke dependencies. All cross-app communication goes through Atlas.
  **Approved exceptions** (documented peer service calls):
  - `Shasta-PRA-Backup → civic_media POST /api/transcribe` — Transcription-as-a-Service
  New cross-spoke calls must be approved and added to this exception list.

## Key Files
- `app/config.py` — Central config, paths, env vars
- `app/db.py` — Async SQLAlchemy engine, session factory, WAL pragmas, FTS5
- `app/models.py` — 10 ORM models (Person, Election, Filer, Filing, Transaction, FilerPerson, TransactionPerson, ElectionCandidate, ScrapeLog, RssFeedState)
- `app/main.py` — FastAPI app factory with lifespan, dashboard endpoints, Jinja2 filters
- `app/utils/process_manager.py` — Zombie process cleanup for port 8855
- `app/services/netfile_api.py` — NetFile Connect2 API client (6 endpoints)
- `app/services/search_indexer.py` — FTS5 rebuild + search with LIKE fallback
- `app/routers/system.py` — Health, port status, zombie kill endpoints
- `app/routers/atlas.py` — Atlas registration + people search stub
- `app/routers/filings.py` — Filing browser, detail, PDF serve (5 endpoints)
- `app/routers/filers.py` — Filer directory + detail (3 endpoints)
- `app/routers/transactions.py` — Transaction search + CSV export (3 endpoints)
- `app/routers/search.py` — Global search HTMX dropdown
- `app/routers/stubs.py` — People/Elections stub pages
- `app/static/js/pdf-viewer.js` — PDF.js slide-out viewer controller
- `run.py` — Startup: kill zombies + SO_REUSEADDR uvicorn
- `BUILDOUT.md` — Complete specification (1,398 lines)

## Commands
- `python run.py` — Start server (kills zombies, binds :8855)
- `python -m app.utils.process_manager --status` — Port diagnostics
- `python -m app.utils.process_manager --kill` — Kill zombies CLI
- `pip install -r requirements.txt` — Install deps

## Conventions
- String UUIDs for all primary keys (cross-database join safety)
- DeclarativeBase (SQLAlchemy 2.0 style)
- Async everywhere (engine, sessions, httpx)
- FTS5 contentless index for global search
- HTMX for frontend interactivity
- No Celery — FastAPI background tasks + SSE for pull operations

## Database
- SQLite at `database/netfile_tracker.db`
- WAL mode + foreign keys + busy_timeout=5000
- FTS5 virtual table `search_index` for global search

## Implementation Phases
- Phase 1: Foundation (DONE) — scaffolding, models, process manager, routers, frontend shell
- Phase 2: Pull Button + Data Pipeline (DONE) — RSS discovery, SSE progress, CAL parser
- Phase 3: Frontend Core (DONE) — filing browser, filer directory, transactions, PDF viewer, global search
- Phase 4: Backfill CLI (DONE) — Excel export, historical data (407 filers, 208 filings, 5,978 txns)
- Phase 5: People & Transactions (stub) — tagging, fuzzy matching
- Phase 6: Atlas Integration (stub) — people search, elections

## Testing

No formal test suite exists yet. Use Playwright for browser-based UI testing and pytest for API/service tests. The `tests/` directory exists but has no test files.

### Setup

```bash
pip install playwright pytest pytest-asyncio httpx
python -m playwright install chromium
```

### Running Tests

```bash
pytest tests/ -v
pytest tests/ -v -k "browser"    # Playwright UI tests only
pytest tests/ -v -k "api"        # API tests only
```

### Writing Tests

- **Browser tests** go in `tests/test_browser.py` — use Playwright to verify the HTMX UI (filing browser, filer directory, transaction search, PDF viewer, global search dropdown)
- **API tests** go in `tests/test_api.py` — use httpx against FastAPI endpoints
- **Service tests** go in `tests/test_services.py` — unit tests for NetFile API client, search indexer, CAL parser
- The server must be running at localhost:8855 for browser tests

### Key Flows to Test

1. **Filing browser**: list loads, filters work, detail shows transactions
2. **PDF viewer**: slide-out viewer opens and renders filing PDFs
3. **Transaction search**: search and CSV export work correctly
4. **Data pull**: SSE progress stream works during NetFile data pull
5. **Global search**: HTMX dropdown returns results across filers/filings/transactions

### Lazy ChromaDB Sync (Atlas RAG)
Atlas maintains a centralized ChromaDB vector store. This project does NOT need its
own vector DB. Atlas fetches candidate records from this spoke's search API, chunks
deterministically, validates against ChromaDB cache, and embeds only new/stale chunks.
ChromaDB is a cache — this spoke's SQLite DB is the source of truth.

See: `Atlas/app/services/rag/deterministic_chunking.py` for this spoke's chunking strategy.

## Master Schema & Codex References

**`E:\0-Automated-Apps\MASTER_SCHEMA.md`** — Canonical cross-project database
schema and API contracts. **HARD RULE: If you add, remove, or modify any database
tables, columns, API endpoints, or response shapes, you MUST update the Master
Schema before finishing your task.** Do not skip this — other projects read it to
understand this project's data contracts.

**`E:\0-Automated-Apps\MASTER_PROJECT.md`** describes the overall ecosystem
architecture and how all projects interconnect.

> **HARD RULE — READ AND UPDATE THE CODEX**
>
> **`E:\0-Automated-Apps\master_codex.md`** is the living interoperability codex.
> 1. **READ it** at the start of any session that touches APIs, schemas, tools,
>    chunking, person models, search, or integration with other projects.
> 2. **UPDATE it** before finishing any task that changes cross-project behavior.
>    This includes: new/changed API endpoints, database schema changes, new tools
>    or tool modifications in Atlas, chunking strategy changes, person model changes,
>    new cross-spoke dependencies, or completing items from a project's outstanding work list.
> 3. **DO NOT skip this.** The codex is how projects stay in sync. If you change
>    something that another project depends on and don't update the codex, the next
>    agent working on that project will build on stale assumptions and break things.
