"""FastAPI application factory with async lifespan."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db import init_db, get_db
from app.config import APP_PORT, ATLAS_SPOKE_NAME
import app.models  # noqa: F401 — register all ORM models with Base before init_db
from app.models import Filing, Filer, Transaction, Person, WatchedFiler  # noqa: F401
from app.routers import system, atlas, scraper
from app.routers import filings as filings_router
from app.routers import filers as filers_router
from app.routers import transactions as transactions_router
from app.routers import stubs as stubs_router
from app.routers import search as search_router
from app.routers import elections as elections_router
from app.routers import settings as settings_router

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB. Shutdown: cleanup."""
    await init_db()
    yield


app = FastAPI(
    title="NetFile Campaign Finance Tracker",
    description="Shasta County campaign finance disclosure tracker — Atlas spoke",
    version="0.1.0",
    docs_url="/api/docs",
    lifespan=lifespan,
)

# Static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Jinja2 custom filters
def number_format(value, fmt="{:,}"):
    try:
        return fmt.format(value)
    except (ValueError, TypeError):
        return str(value)

templates.env.filters["number_format"] = number_format

# Routers
app.include_router(system.router)
app.include_router(atlas.router)
app.include_router(scraper.router)
app.include_router(filings_router.router)
app.include_router(filers_router.router)
app.include_router(transactions_router.router)
app.include_router(elections_router.router)
app.include_router(settings_router.router)
app.include_router(stubs_router.router)
app.include_router(search_router.router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("pages/dashboard.html", {
        "request": request,
        "title": "Dashboard",
    })


@app.get("/dashboard/stats", response_class=HTMLResponse)
async def dashboard_stats(request: Request, db: AsyncSession = Depends(get_db)):
    """HTMX partial — dashboard stat cards with live DB counts."""
    filing_count = (await db.execute(select(func.count(Filing.filing_id)))).scalar() or 0
    filer_count = (await db.execute(select(func.count(Filer.filer_id)))).scalar() or 0
    txn_count = (await db.execute(select(func.count(Transaction.transaction_id)))).scalar() or 0

    # Contributions = Schedule A + C
    contributions_total = (await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.schedule.in_(["A", "C"])
        )
    )).scalar() or 0

    # Expenditures = Schedule E
    expenditures_total = (await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.schedule == "E"
        )
    )).scalar() or 0

    return templates.TemplateResponse("components/dashboard_stats.html", {
        "request": request,
        "filing_count": filing_count,
        "filer_count": filer_count,
        "transaction_count": txn_count,
        "contributions_total": contributions_total,
        "expenditures_total": expenditures_total,
    })


@app.get("/dashboard/recent", response_class=HTMLResponse)
async def dashboard_recent(request: Request, db: AsyncSession = Depends(get_db)):
    """HTMX partial — 10 most recent filings."""
    result = await db.execute(
        select(Filing)
        .options(joinedload(Filing.filer))
        .order_by(Filing.filing_date.desc())
        .limit(10)
    )
    filings = result.unique().scalars().all()

    return templates.TemplateResponse("components/recent_filings.html", {
        "request": request,
        "filings": filings,
    })


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return templates.TemplateResponse("errors/404.html", {
        "request": request,
        "title": "Not Found",
    }, status_code=404)


@app.exception_handler(500)
async def server_error(request: Request, exc):
    return templates.TemplateResponse("errors/500.html", {
        "request": request,
        "title": "Server Error",
    }, status_code=500)
