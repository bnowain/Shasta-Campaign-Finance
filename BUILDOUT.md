# BUILDOUT.md — Shasta NetFile Campaign Finance Tracker

> **Project codename:** `netfile-tracker`
> **Role in Atlas:** Spoke application — campaign finance disclosure data
> **Stack:** FastAPI + async SQLAlchemy + aiosqlite (SQLite) + Jinja2/HTMX + PDF.js
> **Port:** 8855 (consistent with other spokes: civic_media=8833, shasta-db=8844, article-tracker=8866, Facebook-Offline=8877, Shasta-PRA-Backup=8888, Atlas hub=8800)

---

## Claude Code Setup Instructions

When starting a Claude Code session for this project, add the sibling project directories as context so the schema, API patterns, and People system stay compatible:

```
# From the netfile-tracker project root (Shasta-Campaign-Finance directory):
claude --add-dir ../civic_media --add-dir ../Shasta-DB --add-dir ../article-tracker --add-dir ../Atlas --add-dir ../Facebook-Offline --add-dir ../Shasta-PRA-Backup

# Minimal (just schema-critical siblings):
claude --add-dir ../civic_media --add-dir ../Shasta-DB --add-dir ../article-tracker
```

**Key files to reference in sibling projects:**

| Project | Files to Study | Why |
|---------|---------------|-----|
| `civic_media` | `app/models.py`, `app/config.py`, `app/routers/` | Most mature schema — Person model, SQLAlchemy patterns, Celery task patterns |
| `Shasta-DB` | `app/models.py`, `app/main.py`, `templates/` | Person ↔ Instance many-to-many pattern, HTMX two-pane UI, inline preview |
| `article-tracker` | `archiver/models.py`, `archiver/database.py`, `web_ui.py` | News aggregation patterns, card-grid frontend, search/filter UI |
| `Atlas` | `api/`, `models/people.py` | Hub API contract, People search endpoint, cross-spoke registration |
| `Facebook-Offline` | models, config | Social media archiving patterns, anti-detection, Graph API integration |
| `Shasta-PRA-Backup` | models, database | NextRequest mirroring patterns, document storage, PRA request metadata |

**The civic_media `Person` model and shasta-db `Person` model are the reference implementations.** This project's People table must be join-compatible for Atlas People search and RAG queries.

---

## 1. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                                ATLAS HUB (:8800)                             │
│       People Search  │  Cross-Project RAG  │  Workflow Triggers              │
└───┬──────────┬───────────┬───────────┬──────────┬──────────┬────────────────┘
    │          │           │           │          │          │
┌───▼───┐ ┌───▼─────┐ ┌───▼──────┐ ┌─▼────────┐ ┌▼───────┐ ┌▼──────────┐
│civic_ │ │Shasta-DB│ │netfile-  │ │article-  │ │Facebook│ │Shasta-PRA│
│media  │ │         │ │tracker   │ │tracker   │ │Offline │ │Backup    │
│ :8833 │ │  :8844  │ │  :8855   │ │  :8866   │ │ :8877  │ │  :8888   │
│       │ │         │ │← THIS    │ │          │ │        │ │          │
│Meeting│ │Mixed    │ │Campaign  │ │Local news│ │FB page │ │NextReq   │
│transcr│ │civic    │ │finance   │ │aggregator│ │archive │ │PRA mirror│
│iption │ │archive  │ │filings   │ │w/paywall │ │        │ │for LLM   │
│+diari │ │browser  │ │          │ │bypass    │ │        │ │inference │
└───────┘ └─────────┘ └──────────┘ └──────────┘ └────────┘ └──────────┘
  People✓    People✓    People✓     People(*)    People(*)   People(*)

(*) = People table planned but not yet implemented

All spokes share:
- String UUID primary keys for cross-DB compatibility
- Person model with canonical_name, aliases (JSON), entity_type
- Atlas registration endpoint (POST /api/atlas/register)
- People search API (GET /api/people/search?q={query})
- SQLite + WAL mode + FTS5 full-text search
- FastAPI + async SQLAlchemy + HTMX stack (except article-tracker which uses Flask)
```

netfile-tracker internal architecture:

```

┌──────────────────────────────────────────────────────┐
│                   FastAPI App (:8855)                 │
├──────────┬───────────┬───────────┬───────────────────┤
│ Scraper  │ API       │ Frontend  │ Atlas Integration  │
│ Service  │ Routers   │ (HTMX)   │ API                │
├──────────┴───────────┴───────────┴───────────────────┤
│              async SQLAlchemy + aiosqlite             │
├──────────────────────────────────────────────────────┤
│                    SQLite (WAL)                       │
│  filers │ filings │ transactions │ people │ elections │
└──────────────────────────────────────────────────────┘
         │                              │
    ┌────▼──────┐                 ┌─────▼─────┐
    │ PDF Store │                 │  NetFile   │
    │ (local)   │                 │ Connect2   │
    │ /pdfs/    │                 │ Public API │
    └───────────┘                 └────────────┘
```

---

## 2. Directory Structure

```
netfile-tracker/
├── CLAUDE.md                    # Claude Code project context
├── BUILDOUT.md                  # This file
├── README.md
├── requirements.txt
├── .env                         # NETFILE_AID=CSHA, ports, paths
├── .gitignore
│
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app factory, lifespan, mount static
│   ├── config.py                # Settings from .env, path constants
│   ├── db.py                    # Engine, session factory, migrations
│   ├── models.py                # SQLAlchemy ORM models (see §3)
│   │
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── filings.py           # Filing list, detail, search endpoints
│   │   ├── filers.py            # Committee/candidate list + detail
│   │   ├── transactions.py      # Contribution/expenditure search
│   │   ├── people.py            # People CRUD, tagging, Atlas sync
│   │   ├── elections.py         # Election cycle views
│   │   ├── scraper.py           # Pull button endpoints: discover, ingest, SSE stream
│   │   └── atlas.py             # Atlas hub registration + API contract
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── netfile_api.py       # Connect2 public API client
│   │   ├── excel_export.py      # Bulk Excel download + parse (backfill only)
│   │   ├── pdf_downloader.py    # Filing PDF acquisition
│   │   ├── rss_monitor.py       # RSS feed parsing for Pull discovery
│   │   ├── pull_pipeline.py     # Pull button: discover → ingest → SSE progress
│   │   ├── pull_state.py        # In-memory state for SSE progress broadcasting
│   │   └── people_linker.py     # Auto-link transaction names → People
│   │
│   └── static/
│       ├── css/
│       │   └── style.css        # Tailwind-inspired utility + custom
│       ├── js/
│       │   ├── app.js           # HTMX config, PDF viewer init
│       │   ├── pdf-viewer.js    # PDF.js slide-out panel controller
│       │   └── search.js        # Live search debouncing
│       └── vendor/
│           └── pdfjs/           # PDF.js dist (build locally or CDN fallback)
│
├── templates/
│   ├── base.html                # Shell: nav, sidebar, content area
│   ├── components/
│   │   ├── filing_card.html     # Filing card for grid/list views
│   │   ├── transaction_row.html # Transaction table row (HTMX partial)
│   │   ├── person_chip.html     # Clickable person tag chip
│   │   ├── pdf_panel.html       # Slide-out PDF viewer panel
│   │   ├── search_bar.html      # Global search with type-ahead
│   │   └── pagination.html      # Cursor-based pagination controls
│   ├── pages/
│   │   ├── dashboard.html       # Landing: recent filings, stats, alerts
│   │   ├── filings.html         # Filing browser with filters
│   │   ├── filing_detail.html   # Single filing: metadata + transactions + PDF
│   │   ├── filers.html          # Committee/candidate directory
│   │   ├── filer_detail.html    # Single filer: filing history + totals
│   │   ├── transactions.html    # Transaction search/filter view
│   │   ├── people.html          # People directory with cross-project links
│   │   ├── person_detail.html   # Person: all appearances across filings
│   │   ├── elections.html       # Election cycle overview
│   │   └── scraper_status.html  # Pull history + backfill logs
│   └── errors/
│       ├── 404.html
│       └── 500.html
│
├── pdfs/                        # Downloaded filing PDFs
│   └── {filing_id}.pdf          # Keyed by NetFile filing ID
│
├── exports/                     # Downloaded Excel exports (raw)
│   └── CSHA_{year}_{type}.xlsx
│
├── database/
│   └── netfile_tracker.db       # SQLite with WAL mode
│
├── scripts/
│   ├── __init__.py
│   ├── backfill.py              # CLI: python -m scripts.backfill --years 2025 2024
│   ├── backfill_state.py        # Resumable state manager (ScrapeLog wrapper)
│   └── link_people.py           # Batch: fuzzy-match names → People records
│
└── tests/
    ├── test_netfile_api.py
    ├── test_excel_parser.py
    └── test_people_linker.py
```

---

## 3. Database Schema

### Design Principles
- **Person table is cross-project compatible** — same PK pattern as civic_media and shasta-db
- **All IDs are string UUIDs** (matching civic_media convention)
- **`created_at` / `updated_at` use `datetime.now(timezone.utc)`** (not deprecated `utcnow()`)
- **Junction tables use `source` field** for provenance tracking (manual, auto, excel_import, api)
- **FTS5 virtual table** for full-text search across filer names, transaction names, and people

```python
# app/models.py

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Date,
    Text, ForeignKey, Index, UniqueConstraint, LargeBinary
)
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime, timezone
import uuid

Base = declarative_base()

def gen_id():
    return str(uuid.uuid4())

def utcnow():
    return datetime.now(timezone.utc)


# ─── CORE ENTITIES ───────────────────────────────────────────


class Person(Base):
    """
    Cross-project People entity. Schema-compatible with:
    - civic_media.people (person_id, canonical_name, created_at)
    - shasta_db.people (id, name, created_at)

    Atlas People search aggregates across all spokes via this table.
    """
    __tablename__ = "people"

    person_id       = Column(String, primary_key=True, default=gen_id)
    canonical_name  = Column(String, nullable=False, unique=True)
    aliases         = Column(Text)          # JSON array of alternate name forms
    entity_type     = Column(String)        # individual, committee, organization, officeholder
    notes           = Column(Text)
    atlas_synced    = Column(Boolean, default=False)
    created_at      = Column(DateTime, default=utcnow)
    updated_at      = Column(DateTime, default=utcnow, onupdate=utcnow)

    # Relationships
    filer_links     = relationship("FilerPerson", back_populates="person")
    transaction_links = relationship("TransactionPerson", back_populates="person")


class Election(Base):
    """Election cycles from the NetFile portal."""
    __tablename__ = "elections"

    election_id     = Column(String, primary_key=True, default=gen_id)
    date            = Column(Date, nullable=False)
    name            = Column(String, nullable=False)   # "06/02/2026 Primary Election"
    election_type   = Column(String)                   # primary, general, special, udel
    year            = Column(Integer, nullable=False)
    created_at      = Column(DateTime, default=utcnow)

    candidates      = relationship("ElectionCandidate", back_populates="election")


class Filer(Base):
    """
    Campaign committee or candidate filer entity.
    Populated from /api/public/campaign/list/filer endpoint.
    """
    __tablename__ = "filers"

    filer_id        = Column(String, primary_key=True, default=gen_id)
    netfile_filer_id = Column(String, unique=True)     # NetFile's internal filer ID
    local_filer_id  = Column(String)                   # FPPC committee ID (e.g., "1234567")
    sos_filer_id    = Column(String)                   # Secretary of State ID
    name            = Column(String, nullable=False)   # "Tim Garman for District 5 Supervisor 2026"
    filer_type      = Column(String)                   # candidate, measure, pac, party
    status          = Column(String)                   # active, terminated
    office          = Column(String)                   # "District 5 Supervisor"
    jurisdiction    = Column(String)                   # "Shasta County"
    first_filing    = Column(Date)
    last_filing     = Column(Date)
    created_at      = Column(DateTime, default=utcnow)
    updated_at      = Column(DateTime, default=utcnow, onupdate=utcnow)

    filings         = relationship("Filing", back_populates="filer")
    person_links    = relationship("FilerPerson", back_populates="filer")
    election_links  = relationship("ElectionCandidate", back_populates="filer")

    __table_args__ = (
        Index("ix_filers_name", "name"),
        Index("ix_filers_local_id", "local_filer_id"),
    )


class Filing(Base):
    """
    Individual filing (Form 460, 410, 496, 497, 461, 465, 700, etc.).
    One filer has many filings.
    """
    __tablename__ = "filings"

    filing_id       = Column(String, primary_key=True, default=gen_id)
    netfile_filing_id = Column(String, unique=True, nullable=False)  # NetFile's filing ID
    filer_id        = Column(String, ForeignKey("filers.filer_id"), nullable=False)
    form_type       = Column(String, nullable=False)   # "FPPC Form 460", "FPPC Form 410", etc.
    form_name       = Column(String)                   # Human-readable form description
    filing_date     = Column(DateTime, nullable=False)
    period_start    = Column(Date)                     # Reporting period start
    period_end      = Column(Date)                     # Reporting period end
    amendment_seq   = Column(Integer, default=0)       # 0 = original, 1+ = amendment number
    amends_filing   = Column(String)                   # NetFile filing ID this amends
    amended_by      = Column(String)                   # NetFile filing ID that supersedes this
    is_efiled       = Column(Boolean, default=True)
    efiling_vendor  = Column(String)                   # NetFile vendor enum value
    pdf_path        = Column(String)                   # Local path to downloaded PDF
    pdf_size        = Column(Integer)                  # File size in bytes
    pdf_downloaded  = Column(Boolean, default=False)
    data_source     = Column(String, default="api")    # api, excel_export, rss, manual
    raw_data        = Column(Text)                     # JSON dump of full API response
    created_at      = Column(DateTime, default=utcnow)
    updated_at      = Column(DateTime, default=utcnow, onupdate=utcnow)

    filer           = relationship("Filer", back_populates="filings")
    transactions    = relationship("Transaction", back_populates="filing")

    __table_args__ = (
        Index("ix_filings_date", "filing_date"),
        Index("ix_filings_form", "form_type"),
        Index("ix_filings_filer", "filer_id"),
        Index("ix_filings_period", "period_start", "period_end"),
    )


class Transaction(Base):
    """
    Individual financial transaction from a filing.
    Maps to the Excel export columns (Schedule A, B, C, D, E, F, etc.).
    """
    __tablename__ = "transactions"

    transaction_id  = Column(String, primary_key=True, default=gen_id)
    filing_id       = Column(String, ForeignKey("filings.filing_id"), nullable=False)
    schedule        = Column(String)          # A, B1, B2, C, D, E, F, G, H, I
    transaction_type = Column(String)         # monetary_contribution, nonmonetary, expenditure, loan, etc.
    transaction_type_code = Column(String)    # NetFile type code
    entity_name     = Column(String)          # Name on the transaction
    entity_type     = Column(String)          # IND, COM, OTH, PTY, SCC
    first_name      = Column(String)
    last_name       = Column(String)
    city            = Column(String)
    state           = Column(String)
    zip_code        = Column(String)
    employer        = Column(String)
    occupation      = Column(String)
    amount          = Column(Float, nullable=False)
    cumulative_amount = Column(Float)         # YTD or election-cycle cumulative
    transaction_date = Column(Date)
    description     = Column(Text)            # Purpose/description of expenditure
    memo_code       = Column(Boolean, default=False)
    amendment_flag  = Column(String)          # A=add, D=delete, blank=original
    netfile_transaction_id = Column(String)   # If available from API
    data_source     = Column(String, default="excel_export")  # excel_export, api, manual
    raw_data        = Column(Text)            # JSON of original row/record
    created_at      = Column(DateTime, default=utcnow)

    filing          = relationship("Filing", back_populates="transactions")
    person_links    = relationship("TransactionPerson", back_populates="transaction")

    __table_args__ = (
        Index("ix_transactions_filing", "filing_id"),
        Index("ix_transactions_name", "entity_name"),
        Index("ix_transactions_date", "transaction_date"),
        Index("ix_transactions_amount", "amount"),
        Index("ix_transactions_schedule", "schedule"),
        Index("ix_transactions_type", "transaction_type"),
    )


# ─── JUNCTION TABLES ─────────────────────────────────────────


class FilerPerson(Base):
    """Links filers to People records. A filer IS a person/entity."""
    __tablename__ = "filer_people"

    id              = Column(String, primary_key=True, default=gen_id)
    filer_id        = Column(String, ForeignKey("filers.filer_id"), nullable=False)
    person_id       = Column(String, ForeignKey("people.person_id"), nullable=False)
    role            = Column(String)          # candidate, treasurer, principal_officer
    source          = Column(String, default="manual")  # manual, auto, import
    created_at      = Column(DateTime, default=utcnow)

    filer           = relationship("Filer", back_populates="person_links")
    person          = relationship("Person", back_populates="filer_links")

    __table_args__ = (
        UniqueConstraint("filer_id", "person_id", "role", name="uq_filer_person_role"),
    )


class TransactionPerson(Base):
    """Links transaction entity names to People records."""
    __tablename__ = "transaction_people"

    id              = Column(String, primary_key=True, default=gen_id)
    transaction_id  = Column(String, ForeignKey("transactions.transaction_id"), nullable=False)
    person_id       = Column(String, ForeignKey("people.person_id"), nullable=False)
    match_confidence = Column(Float)          # 0.0–1.0 for fuzzy matches
    source          = Column(String, default="auto")  # manual, auto, fuzzy
    created_at      = Column(DateTime, default=utcnow)

    transaction     = relationship("Transaction", back_populates="person_links")
    person          = relationship("Person", back_populates="transaction_links")

    __table_args__ = (
        UniqueConstraint("transaction_id", "person_id", name="uq_transaction_person"),
    )


class ElectionCandidate(Base):
    """Links filers to specific election cycles."""
    __tablename__ = "election_candidates"

    id              = Column(String, primary_key=True, default=gen_id)
    election_id     = Column(String, ForeignKey("elections.election_id"), nullable=False)
    filer_id        = Column(String, ForeignKey("filers.filer_id"), nullable=False)
    office_sought   = Column(String)
    party           = Column(String)
    is_measure      = Column(Boolean, default=False)
    measure_letter  = Column(String)          # "B" for "Measure B"
    position        = Column(String)          # support, oppose (for measures)
    created_at      = Column(DateTime, default=utcnow)

    election        = relationship("Election", back_populates="candidates")
    filer           = relationship("Filer", back_populates="election_links")


# ─── SCRAPER STATE ────────────────────────────────────────────


class ScrapeLog(Base):
    """Tracks scrape runs for resumability and audit trail."""
    __tablename__ = "scrape_log"

    log_id          = Column(String, primary_key=True, default=gen_id)
    scrape_type     = Column(String, nullable=False)  # filer_list, excel_export, rss_poll, pdf_download, filing_info
    status          = Column(String, default="running")  # running, completed, failed, interrupted
    started_at      = Column(DateTime, default=utcnow)
    completed_at    = Column(DateTime)
    items_processed = Column(Integer, default=0)
    items_total     = Column(Integer)
    error_message   = Column(Text)
    parameters      = Column(Text)            # JSON of scrape parameters (year, page, etc.)


class RssFeedState(Base):
    """Tracks last-seen RSS items to avoid re-processing."""
    __tablename__ = "rss_feed_state"

    id              = Column(String, primary_key=True, default=gen_id)
    feed_url        = Column(String, nullable=False, unique=True)
    last_guid       = Column(String)
    last_polled     = Column(DateTime)
    last_build_date = Column(String)          # From RSS <lastBuildDate>


# ─── FTS VIRTUAL TABLE ───────────────────────────────────────
# Created via raw SQL in migrations, not ORM:
#
# CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
#     entity_type,       -- 'filer', 'transaction', 'person', 'filing'
#     entity_id,         -- PK of the source record
#     name,              -- searchable name text
#     context,           -- additional searchable text (description, employer, etc.)
#     content=''         -- contentless (external content)
# );
```

---

## 4. NetFile API Integration

### 4a. Public API Client (`app/services/netfile_api.py`)

No authentication required for public endpoints. All requests go to `https://netfile.com/Connect2/api/`.

```python
# Key endpoints and their usage:

ENDPOINTS = {
    # List all filers for Shasta County (paginated)
    "filer_list": {
        "method": "POST",
        "path": "/api/public/campaign/list/filer",
        "body": {"aid": "CSHA", "currentPageIndex": 0, "pageSize": 100},
        "format": "json",
    },

    # Get filing metadata by filing ID
    "filing_info": {
        "method": "POST",
        "path": "/api/public/filing/info/{filing_id}",
        "body": {"filingId": "{filing_id}"},
        "format": "json",
        "response_fields": [
            "agency", "filerName", "filingDate", "formName",
            "dateStart", "dateEnd", "amendmentSequenceNumber",
            "amends", "amendedBy", "isEfiled", "vendor",
            "localFilerId", "sosFilerId",
        ],
    },

    # Download filing as rendered PDF
    "filing_image": {
        "method": "GET",
        "path": "/api/public/image/{filing_id}",
        "format": "binary",  # Returns PDF bytes
    },

    # Get e-filing structured data (CAL format)
    "efile_data": {
        "method": "POST",
        "path": "/api/public/efile/{filing_id}",
        "body": {"filingId": "{filing_id}"},
        "format": "json",
    },

    # Transaction type lookup table
    "transaction_types": {
        "method": "POST",
        "path": "/api/public/campaign/list/transaction/types",
        "body": {},
        "format": "json",
    },

    # RSS feed — recent filings (last 15 days, up to 1000 items)
    "rss_feed": {
        "method": "GET",
        "path": "/api/public/list/filing/rss/CSHA/campaign.xml",
        "format": "xml",
    },
}
```

### 4b. Excel Export Automation (`app/services/excel_export.py`)

The portal's Excel export uses ASP.NET `__doPostBack`. Requires session management to capture ViewState:

```
Strategy:
1. GET the portal page → extract __VIEWSTATE, __VIEWSTATEGENERATOR, __EVENTVALIDATION
2. POST with the year selector value + __doPostBack target
3. Receive .xlsx file in response
4. Parse with openpyxl

Two export types per year:
- "Export Amended" (most recent version only) — use for current state
- "Export All" (original + all amendments) — use for amendment chain tracking

Download the column key from:
  https://public.netfile.com/pub2/docs/Export_Column_Key.xls

Rate limiting: 1 request per 5 seconds, one year at a time
```

### 4c. RSS Monitor (`app/services/rss_monitor.py`)

```
Feed URL: https://netfile.com/connect2/api/public/list/filing/rss/CSHA/campaign.xml

Each RSS item contains:
  - guid: unique filing identifier
  - link: direct URL to PDF image (https://netfile.com/Connect2/api/public/image/{id})
  - title: filer name
  - description: form type + period ("FPPC Form 460 (1/1/2026 - 2/18/2026)")

Polling strategy:
  - Check every 30 minutes during business hours (8am-6pm PT)
  - Check every 2 hours off-hours
  - Track last_guid and last_build_date to avoid reprocessing
  - On new items: create Filing record, queue PDF download, pull filing_info
```

---

## 5. Frontend Design

### 5a. Design System

Modern, clean, data-dense interface optimized for research workflows. Follows the article-tracker's visual language but adapted for structured financial data.

```
Color palette:
  --bg:           #f8f9fa
  --surface:      #ffffff
  --text:         #1a1a2e
  --text-muted:   #6c757d
  --border:       #e2e8f0
  --accent:       #2563eb    (blue — financial/civic tone)
  --accent-hover: #1d4ed8
  --success:      #059669
  --warning:      #d97706
  --danger:       #dc2626
  --tag-bg:       #eff6ff
  --tag-text:     #1e40af
  --tag-hover:    #dbeafe

Typography:
  --font-sans:  'Inter', system-ui, sans-serif
  --font-mono:  'JetBrains Mono', 'Fira Code', monospace
  Base size: 14px (data-dense UI)

Spacing scale: 4px base (0.25rem increments)

Border radius:
  Cards: 8px
  Buttons: 6px
  Tags/chips: 9999px (pill)
  Inputs: 6px
```

### 5b. Layout

```
┌─────────────────────────────────────────────────────────────┐
│ ┌─ Topbar ────────────────────────────────────────────────┐ │
│ │ 🏛 NetFile Tracker  [Global Search...    ] [🔄 Pull] ⚙ │ │
│ └─────────────────────────────────────────────────────────┘ │
│ ┌─ Sidebar (collapsible) ──┐ ┌─ Main Content ───────────┐ │
│ │                          │ │                           │ │
│ │  📊 Dashboard            │ │                           │ │
│ │  📁 Filings              │ │  [Content area changes    │ │
│ │  👥 Filers               │ │   based on navigation]    │ │
│ │  💰 Transactions         │ │                           │ │
│ │  🏷 People               │ │                           │ │
│ │  🗳 Elections             │ │                           │ │
│ │  ─────────────           │ │                           │ │
│ │  ⚡ Scraper Status       │ │                           │ │
│ │                          │ │                           │ │
│ │  ── Recent Filings ──   │ │                           │ │
│ │  • Tim Garman - 460     │ │                           │ │
│ │  • Measure B - 410      │ │                           │ │
│ │  • Francescut - 410     │ │                           │ │
│ │                          │ │                           │ │
│ └──────────────────────────┘ └───────────────────────────┘ │
│                                                             │
│ ┌─ PDF Slide-Out Panel (hidden by default) ──────────────┐ │
│ │ ← Close   Filing: Form 460 - Tim Garman   📥 Download  │ │
│ │ ┌────────────────────────────────────────────────────┐  │ │
│ │ │                                                    │  │ │
│ │ │              PDF.js Rendered View                   │  │ │
│ │ │              (scrollable, zoomable)                 │  │ │
│ │ │                                                    │  │ │
│ │ └────────────────────────────────────────────────────┘  │ │
│ │ Page: [< 1 of 12 >]   Zoom: [- 100% +]   🔍 Search   │ │
│ └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 5c. PDF Viewing — Slide-Out Panel (Not Lightbox)

**Rationale:** A lightbox/modal blocks interaction with the underlying data. A slide-out panel lets the user read the PDF while still seeing filing metadata, transaction tables, and people tags. This is how modern document-heavy apps (Notion, Linear, legal research tools) handle inline document viewing.

**Implementation: PDF.js embedded viewer in a right-side slide-out panel.**

```
Behavior:
- Click any "View PDF" button → panel slides in from right (50-70% viewport width)
- Panel overlays main content with slight dim on background
- Keyboard: Escape to close, arrow keys for page navigation
- Panel has its own toolbar: page nav, zoom, search-in-PDF, download button
- URL updates to include filing ID fragment (#filing=123) for shareability
- Panel is resizable via drag handle on left edge
- On mobile: panel goes full-screen

Technical:
- Use PDF.js (pdfjs-dist) loaded from /static/vendor/pdfjs/
- Render via canvas for crisp text
- Lazy-load pages as user scrolls
- Cache rendered pages in memory
- PDF served from local /pdfs/{filing_id}.pdf endpoint
- Fallback for non-downloaded PDFs: proxy through NetFile's image API
```

### 5d. Page Designs

**Dashboard (`/`)**
- Filing activity timeline (last 30 days, bar chart by week)
- Recent filings feed (card list, most recent first)
- Top contributors this cycle (aggregated from transactions)
- Upcoming election deadlines
- Scraper health status indicator

**Filings Browser (`/filings`)**
- Filterable by: form type, filer, date range, election cycle, has-PDF
- Two view modes: card grid (default) and compact table
- Each card shows: filer name, form type, filing date, period, amendment badge
- Click card → filing detail OR click PDF icon → slide-out viewer
- Bulk actions: download PDFs, export to CSV

**Filing Detail (`/filings/{id}`)**
- Header: filer name, form type, dates, amendment chain visualization
- Tab sections:
  - **Summary** — key financial totals (contributions received, expenditures, cash on hand)
  - **Transactions** — sortable/filterable table of all transactions from this filing
  - **Amendment History** — linked chain of original → amendments
  - **Raw Data** — JSON view of API response (collapsible)
- "View PDF" button prominent in header → opens slide-out panel
- People chips inline on transaction rows (clickable → person detail)

**Filer Directory (`/filers`)**
- Searchable list of all committees/candidates
- Filter by: type, status, election, office
- Each entry shows: name, ID, office, total filings count, latest filing date
- Click → filer detail with complete filing history + financial summary

**Transactions Search (`/transactions`)**
- Full-text search across all transaction names, employers, descriptions
- Filter by: schedule (A/B/C/D/E/F), type, amount range, date range, filer
- Results table with: date, filer, contributor/payee, amount, type, schedule
- Click name → person detail (if linked) or create-person flow
- Export filtered results to CSV

**People Directory (`/people`)**
- Displays People tagged in this project
- Shows cross-project badge: "Also in: civic_media, shasta-db, article-tracker, Facebook-Offline, Shasta-PRA-Backup"
- Filter by entity_type, search by name/alias
- Person detail shows all appearances: as filer, as contributor, as payee
- "Tag in Atlas" button syncs to hub People search
- Merge duplicate people (select 2+ → merge into canonical)

**Elections (`/elections`)**
- List of election cycles from portal
- Expand each → candidates and measures with their committees
- Filing deadline calendar
- Financial summary per race

### 5e. HTMX Patterns

Follow the established patterns from shasta-db and civic_media:

```html
<!-- Live search with debounce -->
<input type="search"
       name="q"
       hx-get="/filings/search"
       hx-trigger="keyup changed delay:300ms"
       hx-target="#results"
       hx-indicator="#spinner"
       placeholder="Search filings...">

<!-- Infinite scroll pagination -->
<div id="results" hx-get="/filings?page=2"
     hx-trigger="revealed"
     hx-swap="afterend">

<!-- Person chip with tag action -->
<span class="person-chip"
      hx-post="/people/link"
      hx-vals='{"transaction_id": "...", "person_id": "..."}'
      hx-target="closest .person-tags"
      hx-swap="outerHTML">
  Patrick Jones ✕
</span>

<!-- PDF panel trigger -->
<button hx-get="/filings/{id}/pdf-panel"
        hx-target="#pdf-panel"
        hx-swap="innerHTML"
        hx-on::after-swap="openPdfPanel()">
  📄 View PDF
</button>
```

---

## 6. People System & Atlas Integration

### 6a. People Tagging Workflow

```
1. AUTOMATIC (on Excel import):
   - Parse entity_name from each transaction
   - Normalize: "JONES, PATRICK M" → "Patrick M Jones"
   - Fuzzy match against existing People (Levenshtein + phonetic)
   - High confidence (>0.95): auto-link with source="auto"
   - Medium confidence (0.80-0.95): flag for review
   - Low confidence (<0.80): leave unlinked

2. MANUAL (in UI):
   - Transaction row shows entity_name + person chip area
   - Click "+" to search/create Person record
   - Type-ahead search against People table
   - "Create New Person" if no match
   - Person chip appears, linked with source="manual"

3. BATCH (via script):
   - scripts/link_people.py processes all unlinked transactions
   - Groups by normalized name → creates Person records for clusters
   - Flags ambiguous cases for manual review
```

### 6b. Atlas People Search Contract

The Atlas hub queries each spoke's `/api/people/search` endpoint:

```python
# app/routers/atlas.py

@router.get("/api/people/search")
async def search_people(
    q: str,
    limit: int = 20,
    db: AsyncSession = Depends(get_db)
):
    """
    Atlas People search endpoint.
    Returns people matching query with their appearances in this project.
    """
    results = await db.execute(
        select(Person)
        .where(
            or_(
                Person.canonical_name.ilike(f"%{q}%"),
                Person.aliases.ilike(f"%{q}%"),
            )
        )
        .limit(limit)
    )
    people = results.scalars().all()

    return {
        "source": "netfile-tracker",
        "results": [
            {
                "person_id": p.person_id,
                "canonical_name": p.canonical_name,
                "entity_type": p.entity_type,
                "appearances": {
                    "as_filer": len(p.filer_links),
                    "as_transaction_party": len(p.transaction_links),
                },
                "detail_url": f"http://localhost:8855/people/{p.person_id}",
            }
            for p in people
        ],
    }


@router.post("/api/atlas/register")
async def register_with_atlas():
    """Register this spoke with the Atlas hub."""
    return {
        "name": "netfile-tracker",
        "type": "campaign_finance",
        "port": 8855,
        "endpoints": {
            "people_search": "/api/people/search",
            "health": "/api/health",
        },
        "description": "Shasta County NetFile campaign finance disclosures",
    }
```

### 6c. Cross-Project Person Resolution

When a Person record exists in multiple spokes, Atlas maintains a mapping table. The `person_id` is locally generated in each spoke, and Atlas maintains an equivalence map:

```
Atlas hub keeps:
  atlas_person_id → [(spoke="civic_media", person_id="abc-123"),
                     (spoke="netfile-tracker", person_id="def-456"),
                     (spoke="shasta-db", person_id="ghi-789"),
                     (spoke="article-tracker", person_id="jkl-012"),
                     (spoke="Facebook-Offline", person_id="mno-345"),
                     (spoke="Shasta-PRA-Backup", person_id="pqr-678")]

This means "Tim Garman" appearing in:
  - civic_media (as a speaker in board meetings)
  - netfile-tracker (as a campaign filer)
  - shasta-db (in public records)
...all resolve to the same Atlas person.
```

---

## 7. Data Acquisition — Two Modes

There are exactly **two ways** data enters the system. They are completely separate:

| | **Pull Button (UI)** | **Backfill CLI (Terminal)** |
|---|---|---|
| **Triggered by** | Click "🔄 Pull" in header | `python -m scripts.backfill --years 2025 2024` |
| **Direction** | Forward only — new filings since last pull | Historical — specified year(s) |
| **Data source** | RSS feed + filing_info API + PDF download | Excel bulk export + filing_info API + PDF download |
| **Scope** | Whatever's new since last poll | All filings for the requested year(s) |
| **UI feedback** | Progress bar in header | Terminal stdout progress |
| **Duration** | Seconds to a few minutes | Minutes to hours per year |
| **Safe to run concurrently** | No — acquires a lock | No — acquires the same lock |

---

### 7a. Pull Button — Forward Polling (UI)

The Pull button lives in the topbar header and is always visible. It follows a three-phase UX flow: **discover → confirm → ingest**.

**Header States:**

```
IDLE:
┌──────────────────────────────────────────────────────────────┐
│ 🏛 NetFile Tracker  [Global Search...    ] [🔄 Pull]    ⚙  │
└──────────────────────────────────────────────────────────────┘

DISCOVERING (Phase 1 — checking RSS + API for new items):
┌──────────────────────────────────────────────────────────────┐
│ 🏛 NetFile Tracker  [Global Search...    ] [⏳ Checking...]  │
└──────────────────────────────────────────────────────────────┘

DISCOVERED (Phase 2 — showing what was found):
┌──────────────────────────────────────────────────────────────┐
│ 🏛 NetFile Tracker  [Global Search ]  Found 4 new filings   │
│                                       [✅ Pull Now] [✕]     │
└──────────────────────────────────────────────────────────────┘
  (if 0 found, show "Already up to date ✓" for 3s then return to IDLE)

INGESTING (Phase 3 — downloading + processing):
┌──────────────────────────────────────────────────────────────┐
│ 🏛 NetFile Tracker  Pulling 2/4: Tim Garman - Form 460      │
│ ████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  50%  [✕]   │
└──────────────────────────────────────────────────────────────┘

COMPLETE:
┌──────────────────────────────────────────────────────────────┐
│ 🏛 NetFile Tracker  ✓ Pulled 4 filings (3 PDFs)  [Dismiss]  │
└──────────────────────────────────────────────────────────────┘
  (auto-dismiss after 8s, return to IDLE)

ERROR:
┌──────────────────────────────────────────────────────────────┐
│ 🏛 NetFile Tracker  ⚠ Pull failed: connection timeout [Retry]│
└──────────────────────────────────────────────────────────────┘
```

**Backend Flow:**

```
Phase 1 — DISCOVER (/api/pull/discover, GET)
  1. Fetch RSS feed: https://netfile.com/connect2/api/public/list/filing/rss/CSHA/campaign.xml
  2. Parse all <item> entries
  3. Compare each guid against rss_feed_state.last_guid and filings.netfile_filing_id
  4. Return list of NEW items not already in the database
  5. Response:
     {
       "new_filings": [
         {
           "guid": "42008bcf-...",
           "netfile_filing_id": "215761012",
           "filer_name": "Tim Garman for District 5 Supervisor 2026",
           "form_description": "FPPC Form 460 (1/1/2026 - 2/18/2026)",
           "pdf_url": "https://netfile.com/Connect2/api/public/image/215761012"
         },
         ...
       ],
       "count": 4
     }

Phase 2 — User sees count, clicks "Pull Now" (or dismisses)

Phase 3 — INGEST (/api/pull/ingest, POST)
  Accepts the list of filing IDs to pull. Runs as a background task
  with SSE (Server-Sent Events) progress stream.

  For each new filing:
    a. POST /api/public/filing/info/{filing_id}
       → Create/update Filing record (form_type, dates, filer, amendment chain)
       → Create/update Filer record if filer is new
    b. GET /api/public/image/{filing_id}
       → Download PDF to pdfs/{filing_id}.pdf
       → Update Filing.pdf_path, pdf_size, pdf_downloaded
    c. POST /api/public/efile/{filing_id}
       → If e-filed, pull structured transaction data
       → Create Transaction records
       → Run people_linker on new transactions
    d. Update rss_feed_state with latest guid + timestamp
    e. Emit SSE progress event:
       {
         "current": 2,
         "total": 4,
         "filing_name": "Tim Garman - Form 460",
         "phase": "downloading_pdf",  // or "fetching_metadata", "linking_people"
         "percent": 50
       }

  On completion:
    Emit SSE complete event with summary:
    {
      "status": "complete",
      "filings_added": 4,
      "pdfs_downloaded": 3,
      "transactions_added": 127,
      "people_linked": 34,
      "elapsed_seconds": 18.4
    }
```

**HTMX Implementation:**

```html
<!-- Pull button in base.html header -->
<div id="pull-container" class="pull-widget">
  <button id="pull-btn"
          hx-get="/api/pull/discover"
          hx-target="#pull-container"
          hx-swap="innerHTML"
          hx-indicator="#pull-spinner"
          class="pull-button">
    <span id="pull-spinner" class="htmx-indicator">⏳</span>
    <span class="pull-idle">🔄 Pull</span>
  </button>
</div>

<!-- Server returns this partial when new items found -->
<!-- templates/components/pull_discovered.html -->
<div class="pull-discovered">
  <span class="pull-count">Found {{ count }} new filing{{ 's' if count != 1 }}</span>
  <button hx-post="/api/pull/ingest"
          hx-vals='{{ filing_ids_json }}'
          hx-target="#pull-container"
          hx-swap="innerHTML"
          class="pull-confirm">✅ Pull Now</button>
  <button hx-get="/api/pull/dismiss"
          hx-target="#pull-container"
          hx-swap="innerHTML"
          class="pull-dismiss">✕</button>
</div>

<!-- Server returns this partial for progress (SSE-driven) -->
<!-- templates/components/pull_progress.html -->
<div class="pull-progress"
     hx-ext="sse"
     sse-connect="/api/pull/stream"
     sse-swap="progress"
     hx-target="#pull-progress-inner">
  <div id="pull-progress-inner">
    <span class="pull-status">Starting...</span>
    <div class="progress-bar">
      <div class="progress-fill" style="width: 0%"></div>
    </div>
  </div>
</div>

<!-- SSE updates replace #pull-progress-inner with current state -->
<!-- On sse-swap="complete", replace entire #pull-container with summary -->
```

**SSE Endpoint:**

```python
# app/routers/scraper.py

from sse_starlette.sse import EventSourceResponse

@router.get("/api/pull/stream")
async def pull_stream(request: Request):
    """SSE stream for pull progress updates."""
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            state = pull_state_manager.get_current()
            if state["status"] == "idle":
                break
            yield {
                "event": state["status"],  # "progress" or "complete"
                "data": json.dumps(state),
            }
            if state["status"] == "complete":
                break
            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())
```

**Concurrency Lock:**

```python
# app/services/scraper_pipeline.py

import asyncio

_pull_lock = asyncio.Lock()

async def run_pull(filing_ids: list[str], db: AsyncSession):
    if _pull_lock.locked():
        raise HTTPException(409, "A pull is already in progress")

    async with _pull_lock:
        # ... do the work ...
```

---

### 7b. Backfill CLI — Historical Data (`scripts/backfill.py`)

Completely separate from the UI. Run from terminal. Designed for overnight execution.

**Usage:**

```powershell
# Pull specific years
python -m scripts.backfill --years 2026 2025

# Pull a range
python -m scripts.backfill --years 2020-2026

# Pull everything available (1997-present)
python -m scripts.backfill --all

# Pull only Excel transaction data (skip PDFs)
python -m scripts.backfill --years 2025 --skip-pdfs

# Pull only PDFs for filings already in DB that are missing PDFs
python -m scripts.backfill --pdfs-only

# Pull only filer list (refresh committee directory)
python -m scripts.backfill --filers-only

# Dry run (show what would be fetched, don't write)
python -m scripts.backfill --years 2025 --dry-run

# Resume interrupted backfill (reads ScrapeLog for last position)
python -m scripts.backfill --resume
```

**Backfill Pipeline (per year):**

```
Step 1 — FILER LIST (if not already populated or --filers-only)
  POST /api/public/campaign/list/filer {aid: "CSHA", pageSize: 100}
  Paginate through all results
  Upsert into filers table
  stdout: "Filers: 142 total (8 new)"

Step 2 — EXCEL EXPORT DOWNLOAD
  a. GET portal page → extract __VIEWSTATE, __EVENTVALIDATION
  b. Set year dropdown to target year
  c. POST "Export Amended" → receive .xlsx
  d. Save raw file to exports/CSHA_{year}_amended.xlsx
  e. Parse with openpyxl
  f. For each row:
     - Map columns via Export_Column_Key
     - Find or create Filing record from filing reference
     - Find or create Filer record from committee ID
     - Create Transaction record (skip if duplicate by composite key)
  g. stdout progress: "2025: 1,247/1,247 transactions [████████████████] 100%"
  Rate: 1 export request per 10 seconds between years

Step 3 — FILING METADATA ENRICHMENT
  For each Filing record missing form_type or amendment data:
    POST /api/public/filing/info/{filing_id}
    Update filing metadata
  Rate: 2 requests/second with 0.5-1.5s jitter
  stdout: "Filing metadata: 89/89 [████████████████] 100%"

Step 4 — PDF DOWNLOADS (unless --skip-pdfs)
  For each Filing where pdf_downloaded=False:
    GET /api/public/image/{filing_id}
    Save to pdfs/{filing_id}.pdf
    Update Filing record
  Rate: 1 download per 3 seconds
  stdout: "PDFs: 72/89 [████████████████] 100% (17 paper-only, no PDF)"

Step 5 — ELECTION MAPPING
  Parse election list from portal page HTML
  Create/update Election records
  Link filers to elections via ElectionCandidate

Step 6 — PEOPLE LINKING
  Run people_linker batch on all unlinked transactions from this year
  stdout: "People: linked 834/1,247 transactions to 156 people (413 unlinked)"

Step 7 — SUMMARY
  ═══════════════════════════════════════════
  BACKFILL COMPLETE: 2025
  ═══════════════════════════════════════════
    Filers:        142 (8 new)
    Filings:        89 (89 new)
    Transactions: 1,247 (1,247 new)
    PDFs:           72 downloaded (17 paper-only)
    People:        156 linked (413 unlinked)
    Duration:     12m 34s
    Database:     ./database/netfile_tracker.db (14.2 MB)
  ═══════════════════════════════════════════
```

**Resumability:**

```python
# scripts/backfill.py

# All operations write ScrapeLog entries with:
#   scrape_type: "backfill_excel_{year}", "backfill_pdfs_{year}", etc.
#   parameters: JSON with cursor position
#   status: running → completed | failed | interrupted
#
# On --resume:
#   1. Find most recent ScrapeLog with status != "completed"
#   2. Read parameters to determine where it left off
#   3. Continue from that position
#
# On KeyboardInterrupt (Ctrl+C):
#   1. Set current ScrapeLog status = "interrupted"
#   2. Save current cursor position to parameters
#   3. Print "Interrupted. Run with --resume to continue."
#   4. Exit cleanly (no partial transaction commits)

class BackfillState:
    """Manages resumable backfill state via ScrapeLog table."""

    async def start(self, scrape_type: str, total: int, params: dict) -> str:
        """Create a new ScrapeLog entry. Returns log_id."""

    async def update(self, log_id: str, processed: int):
        """Update items_processed count."""

    async def complete(self, log_id: str):
        """Mark as completed with timestamp."""

    async def fail(self, log_id: str, error: str):
        """Mark as failed with error message."""

    async def interrupt(self, log_id: str, cursor: dict):
        """Mark as interrupted, save cursor position for resume."""

    async def get_resume_point(self, scrape_type: str) -> Optional[dict]:
        """Get cursor position from last interrupted/failed run."""
```

**Concurrency with Pull Button:**

```python
# Both Pull and Backfill acquire the same file-based lock:
#   database/netfile_tracker.lock
#
# This prevents:
#   - Two backfills running simultaneously
#   - A pull running during a backfill
#   - A backfill running during a pull
#
# The lock file contains the PID and start time of the holder.
# Stale locks (PID no longer running) are automatically cleared.
```

---

### 7c. Scraper State Management

```python
# ScrapeLog tracks every acquisition operation for audit + resume:

class ScrapeLog(Base):
    __tablename__ = "scrape_log"

    log_id          = Column(String, primary_key=True, default=gen_id)
    scrape_type     = Column(String, nullable=False)
    #   Types: "pull", "backfill_filers", "backfill_excel_{year}",
    #          "backfill_metadata_{year}", "backfill_pdfs_{year}",
    #          "backfill_people_{year}"
    status          = Column(String, default="running")
    #   Values: running, completed, failed, interrupted
    started_at      = Column(DateTime, default=utcnow)
    completed_at    = Column(DateTime)
    items_processed = Column(Integer, default=0)
    items_total     = Column(Integer)
    items_new       = Column(Integer, default=0)    # actually new records created
    items_skipped   = Column(Integer, default=0)    # duplicates or already-existing
    error_message   = Column(Text)
    parameters      = Column(Text)  # JSON: year, page cursor, last filing ID, etc.
```

---

## 8. Implementation Phases

### Phase 1 — Foundation (Week 1)
- [ ] Project scaffold: FastAPI app, config, models, migrations
- [ ] NetFile API client with all public endpoints
- [ ] Filer list scraper (populate filers table)
- [ ] Basic frontend shell: sidebar nav, topbar with Pull button placeholder, dashboard placeholder
- [ ] SQLite database with WAL mode + FTS5 search index
- [ ] CLAUDE.md for Claude Code sessions

### Phase 2 — Pull Button + Data Pipeline (Week 2)
- [ ] RSS feed parser + discovery endpoint (`/api/pull/discover`)
- [ ] Filing metadata fetcher (filing_info API)
- [ ] PDF downloader service
- [ ] SSE progress stream (`/api/pull/stream`)
- [ ] Pull button HTMX flow: discover → confirm → ingest → progress → complete
- [ ] ScrapeLog state tracking + concurrency lock
- [ ] People auto-linker (runs on each pull)

### Phase 3 — Frontend Core (Week 3)
- [ ] Dashboard with recent filings + activity chart
- [ ] Filings browser (card grid + table view + filters)
- [ ] Filing detail page with transaction table
- [ ] Filer directory + filer detail pages
- [ ] Global search (FTS5-backed)
- [ ] PDF.js slide-out viewer panel

### Phase 4 — Backfill CLI (Week 3-4, parallel)
- [ ] Excel export downloader (ASP.NET form token extraction)
- [ ] Excel parser with Export_Column_Key mapping
- [ ] `python -m scripts.backfill` CLI with --years, --all, --skip-pdfs, --resume
- [ ] Resumable state manager (BackfillState)
- [ ] Terminal progress bars + summary report
- [ ] Election mapping from portal HTML

### Phase 5 — People & Transactions (Week 4)
- [ ] People CRUD interface
- [ ] Manual person tagging on transaction rows
- [ ] Transaction search/filter page
- [ ] People directory with cross-project badges
- [ ] Person merge workflow

### Phase 6 — Atlas Integration (Week 5)
- [ ] Atlas spoke registration endpoint
- [ ] People search API endpoint
- [ ] Cross-project person resolution
- [ ] Elections page with candidate/measure mapping
- [ ] Export capabilities (CSV, filtered datasets)

---

## 9. Configuration

### `.env`

```env
# NetFile
NETFILE_AID=CSHA
NETFILE_API_BASE=https://netfile.com/Connect2/api
NETFILE_PORTAL_URL=https://public.netfile.com/pub2/?AID=CSHA

# Application
APP_PORT=8855
APP_HOST=0.0.0.0
DATABASE_PATH=./database/netfile_tracker.db
PDF_STORAGE_PATH=./pdfs
EXPORT_STORAGE_PATH=./exports

# Scraper
SCRAPE_RATE_LIMIT=2.0        # requests per second
PDF_DOWNLOAD_DELAY=3.0       # seconds between PDF downloads
RSS_POLL_INTERVAL=1800        # 30 minutes

# Atlas Hub (when available)
ATLAS_HUB_URL=http://localhost:8800
ATLAS_SPOKE_NAME=netfile-tracker

# Logging
LOG_LEVEL=INFO
LOG_FILE=./logs/netfile_tracker.log
```

### `requirements.txt`

```
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
sqlalchemy>=2.0
aiosqlite>=0.19.0
jinja2>=3.1
python-dotenv>=1.0
httpx>=0.25.0               # async HTTP client for API calls
sse-starlette>=1.6.0        # Server-Sent Events for pull progress stream
openpyxl>=3.1.0             # Excel parsing
feedparser>=6.0             # RSS parsing
beautifulsoup4>=4.12        # HTML parsing for portal scraping
lxml>=4.9                   # XML parsing
thefuzz>=0.20               # Fuzzy string matching for People linker
python-multipart>=0.0.6     # File uploads
aiofiles>=23.0              # Async file operations
```

---

## 10. Key Decisions & Rationale

| Decision | Rationale |
|----------|-----------|
| **Pull button (forward) vs Backfill CLI (historical)** | Forward polling is lightweight (RSS + a few API calls) and benefits from UI feedback. Historical backfill is heavy (Excel downloads, hundreds of PDFs) and runs overnight — no UI needed. Sharing a concurrency lock prevents conflicts. |
| **SSE for pull progress, not polling** | Server-Sent Events give real-time progress without the client hammering a status endpoint. HTMX has native SSE support via `hx-ext="sse"`. One persistent connection, server pushes updates. |
| **Discover → Confirm → Ingest flow** | Shows the user what's new before doing anything. Prevents accidental pulls during maintenance windows. Also lets the user see "already up to date" without waiting for a full scrape. |
| **Slide-out panel, not lightbox** for PDF viewing | Allows simultaneous reference to filing metadata while reading PDF. Lightbox blocks the underlying context. |
| **PDF.js, not `<iframe>` or `<embed>`** | Cross-browser consistent rendering, in-app search, programmatic page control, no browser PDF plugin dependencies. |
| **Excel export as primary data source** | Transaction-level data in one bulk download per year. API doesn't expose transaction search without credentials. |
| **RSS for real-time monitoring** | 15-day rolling window catches everything. No auth needed. Lightweight polling. |
| **String UUIDs for all PKs** | Consistent with civic_media and shasta-db. Enables future cross-database joins without collision. |
| **FTS5 contentless index** | Decouples search from storage. Rebuild-friendly. Matches shasta-db pattern. |
| **httpx over requests** | Async-native. Consistent with FastAPI's async patterns. Connection pooling built in. |
| **No Celery (initially)** | Pull uses FastAPI background tasks + SSE. Backfill is a standalone CLI script. No worker queue needed. Add Celery later only if Atlas workflow triggers require it. |
| **Local PDF storage** | PDFs are public records, small files (~100KB-2MB). Local storage enables instant viewing without re-fetching from NetFile. |
