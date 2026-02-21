"""Pull endpoints — discover, confirm, ingest with SSE progress."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from app.db import AsyncSessionLocal
from app.services.netfile_api import NetFileClient
from app.services.pull_pipeline import is_pull_running, run_ingest
from app.services.pull_state import pull_state
from app.services.rss_monitor import discover_new_filings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pull", tags=["pull"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Module-level cache of discovered filings (between discover and ingest steps)
_discovered_cache: list = []


@router.get("/discover", response_class=HTMLResponse)
async def discover(request: Request):
    """Phase 1: Check RSS feed for new filings."""
    global _discovered_cache

    if is_pull_running():
        return templates.TemplateResponse("components/pull_progress.html", {
            "request": request,
        })

    pull_state.set_discovering()

    try:
        client = NetFileClient()
        async with AsyncSessionLocal() as db:
            found = await discover_new_filings(client, db)
        await client.close()
    except Exception as e:
        logger.error("Discovery failed: %s", e)
        pull_state.set_error(str(e))
        return templates.TemplateResponse("components/pull_error.html", {
            "request": request,
            "message": str(e),
        })

    if not found:
        pull_state.set_idle()
        return templates.TemplateResponse("components/pull_idle.html", {
            "request": request,
        })

    _discovered_cache = found
    pull_state.set_discovered(len(found))

    return templates.TemplateResponse("components/pull_discovered.html", {
        "request": request,
        "count": len(found),
    })


@router.post("/ingest", response_class=HTMLResponse)
async def ingest(request: Request, background_tasks: BackgroundTasks):
    """Phase 2: Start background ingest of discovered filings."""
    global _discovered_cache

    if is_pull_running():
        return templates.TemplateResponse("components/pull_progress.html", {
            "request": request,
        })

    if not _discovered_cache:
        pull_state.set_idle()
        return templates.TemplateResponse("components/pull_idle.html", {
            "request": request,
        })

    # Grab the cache and clear it
    to_ingest = list(_discovered_cache)
    _discovered_cache = []

    pull_state.start_timer()
    background_tasks.add_task(run_ingest, to_ingest)

    return templates.TemplateResponse("components/pull_progress.html", {
        "request": request,
    })


@router.get("/stream")
async def stream():
    """SSE stream of pull progress events."""

    async def event_generator():
        while True:
            state = pull_state.get_current()
            status = state["status"]

            if status == "ingesting":
                yield {"event": "ingesting", "data": json.dumps(state)}
            elif status == "complete":
                yield {"event": "complete", "data": json.dumps(state)}
                return
            elif status == "error":
                yield {"event": "error_state", "data": json.dumps(state)}
                return
            elif status == "idle":
                return

            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())


@router.get("/complete", response_class=HTMLResponse)
async def complete(request: Request):
    """Render the completion summary partial."""
    state = pull_state.get_current()
    return templates.TemplateResponse("components/pull_complete.html", {
        "request": request,
        "filings": state["filings_ingested"],
        "pdfs": state["pdfs_downloaded"],
        "transactions": state["transactions_created"],
        "elapsed": state["elapsed"],
    })


@router.get("/error", response_class=HTMLResponse)
async def error(request: Request):
    """Render the error partial."""
    state = pull_state.get_current()
    return templates.TemplateResponse("components/pull_error.html", {
        "request": request,
        "message": state["error_message"],
    })


@router.get("/dismiss", response_class=HTMLResponse)
async def dismiss(request: Request):
    """Reset state and return the idle Pull button."""
    pull_state.set_idle()
    return templates.TemplateResponse("components/pull_idle.html", {
        "request": request,
    })
