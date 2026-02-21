# NetFile Campaign Finance Tracker

## Project Overview
Shasta County campaign finance disclosure tracker. Spoke app in the Atlas hub-and-spoke ecosystem.

- **Codename:** netfile-tracker
- **Port:** 8855
- **Stack:** FastAPI + async SQLAlchemy + aiosqlite + Jinja2/HTMX
- **Data source:** NetFile Connect2 public API (agency ID: CSHA)

## Architecture
- Atlas spoke (hub at :8800)
- Sibling spokes: civic_media (:8833), Shasta-DB (:8844), article-tracker (:8866), Facebook-Offline (:8877), Shasta-PRA-Backup (:8888)
- Person model is schema-compatible across all spokes for Atlas People search

## Key Files
- `app/config.py` — Central config, paths, env vars
- `app/db.py` — Async SQLAlchemy engine, session factory, WAL pragmas, FTS5
- `app/models.py` — 10 ORM models (Person, Election, Filer, Filing, Transaction, FilerPerson, TransactionPerson, ElectionCandidate, ScrapeLog, RssFeedState)
- `app/main.py` — FastAPI app factory with lifespan
- `app/utils/process_manager.py` — Zombie process cleanup for port 8855
- `app/services/netfile_api.py` — NetFile Connect2 API client (6 endpoints)
- `app/routers/system.py` — Health, port status, zombie kill endpoints
- `app/routers/atlas.py` — Atlas registration + people search stub
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
- Phase 2: Pull Button + Data Pipeline
- Phase 3: Frontend Core (filing browser, detail, PDF viewer)
- Phase 4: Backfill CLI (Excel export, historical data)
- Phase 5: People & Transactions (tagging, fuzzy matching)
- Phase 6: Atlas Integration (people search, elections)
