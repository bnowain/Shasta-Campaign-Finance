"""Settings router — watched filers, check elections/filings with SSE progress."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.db import get_db
from app.models import WatchedFiler
from app.services.settings_state import settings_state
from app.services.settings_tasks import is_task_running, run_check_elections, run_check_filings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Main settings page."""
    watched = (await db.execute(
        select(WatchedFiler).order_by(WatchedFiler.created_at.desc())
    )).scalars().all()

    return templates.TemplateResponse("pages/settings.html", {
        "request": request,
        "title": "Settings",
        "watched_filers": watched,
    })


# ─── Watched Filers ─────────────────────────────────────────

@router.get("/watched-filers/list", response_class=HTMLResponse)
async def watched_filers_list(request: Request, db: AsyncSession = Depends(get_db)):
    """HTMX partial: watched filers table rows."""
    watched = (await db.execute(
        select(WatchedFiler).order_by(WatchedFiler.created_at.desc())
    )).scalars().all()
    return templates.TemplateResponse("components/settings_watched_rows.html", {
        "request": request,
        "watched_filers": watched,
    })


@router.post("/watched-filers/add", response_class=HTMLResponse)
async def watched_filers_add(request: Request, db: AsyncSession = Depends(get_db)):
    """Add a watched filer name."""
    form = await request.form()
    name = str(form.get("name", "")).strip()

    if name:
        # Check for duplicate
        existing = (await db.execute(
            select(WatchedFiler).where(WatchedFiler.name == name)
        )).scalars().first()

        if not existing:
            wf = WatchedFiler(name=name, notes=str(form.get("notes", "")).strip() or None)
            db.add(wf)
            await db.commit()

    watched = (await db.execute(
        select(WatchedFiler).order_by(WatchedFiler.created_at.desc())
    )).scalars().all()
    return templates.TemplateResponse("components/settings_watched_rows.html", {
        "request": request,
        "watched_filers": watched,
    })


@router.delete("/watched-filers/{filer_id}", response_class=HTMLResponse)
async def watched_filers_delete(
    filer_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """Remove a watched filer."""
    wf = (await db.execute(
        select(WatchedFiler).where(WatchedFiler.id == filer_id)
    )).scalars().first()
    if wf:
        await db.delete(wf)
        await db.commit()

    watched = (await db.execute(
        select(WatchedFiler).order_by(WatchedFiler.created_at.desc())
    )).scalars().all()
    return templates.TemplateResponse("components/settings_watched_rows.html", {
        "request": request,
        "watched_filers": watched,
    })


# ─── Check Elections ─────────────────────────────────────────

@router.post("/check-elections", response_class=HTMLResponse)
async def check_elections(request: Request, background_tasks: BackgroundTasks):
    """Trigger Check Elections background task."""
    if is_task_running():
        return templates.TemplateResponse("components/settings_elections_progress.html", {
            "request": request,
        })

    background_tasks.add_task(run_check_elections)
    return templates.TemplateResponse("components/settings_elections_progress.html", {
        "request": request,
    })


# ─── Check Filings ───────────────────────────────────────────

@router.post("/check-filings", response_class=HTMLResponse)
async def check_filings(request: Request, background_tasks: BackgroundTasks):
    """Trigger Check Filings background task."""
    if is_task_running():
        return templates.TemplateResponse("components/settings_filings_progress.html", {
            "request": request,
        })

    background_tasks.add_task(run_check_filings)
    return templates.TemplateResponse("components/settings_filings_progress.html", {
        "request": request,
    })


# ─── SSE Stream ──────────────────────────────────────────────

@router.get("/stream")
async def stream():
    """SSE stream for settings background task progress."""

    async def event_generator():
        while True:
            state = settings_state.get_current()
            status = state["status"]

            if status == "running":
                yield {"event": "progress", "data": json.dumps(state)}
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


# ─── Task Result Partials ────────────────────────────────────

@router.get("/task-complete", response_class=HTMLResponse)
async def task_complete(request: Request):
    """Render the task completion summary partial."""
    state = settings_state.get_current()
    task_type = state.get("task_type", "elections")
    template = f"components/settings_{task_type}_complete.html"
    return templates.TemplateResponse(template, {
        "request": request,
        **state,
    })


@router.get("/task-error", response_class=HTMLResponse)
async def task_error(request: Request):
    """Render the error partial."""
    state = settings_state.get_current()
    return templates.TemplateResponse("components/settings_error.html", {
        "request": request,
        "message": state["error_message"],
    })


@router.get("/task-dismiss", response_class=HTMLResponse)
async def task_dismiss(request: Request):
    """Reset state and return the idle button."""
    task_type = settings_state.get_current().get("task_type", "elections")
    settings_state.set_idle()
    template = f"components/settings_{task_type}_idle.html"
    return templates.TemplateResponse(template, {
        "request": request,
    })
