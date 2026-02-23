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

## Master Schema Reference

**`E:\0-Automated-Apps\MASTER_SCHEMA.md`** contains the canonical cross-project
database schema. If you add, remove, or modify any database tables or fields in
this project, **you must update the Master Schema** to keep it in sync. The agent
is authorized and encouraged to edit that file directly.

**`E:\0-Automated-Apps\MASTER_PROJECT.md`** describes the overall ecosystem
architecture and how all projects interconnect.
