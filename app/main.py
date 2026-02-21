"""FastAPI application factory with async lifespan."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from app.db import init_db
from app.config import APP_PORT, ATLAS_SPOKE_NAME
import app.models  # noqa: F401 — register all ORM models with Base before init_db
from app.routers import system, atlas, scraper

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

# Routers
app.include_router(system.router)
app.include_router(atlas.router)
app.include_router(scraper.router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("pages/dashboard.html", {
        "request": request,
        "title": "Dashboard",
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
