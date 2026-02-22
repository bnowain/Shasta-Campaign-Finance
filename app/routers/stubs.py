"""Stub pages for Phase 5 (People)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["stubs"])


@router.get("/people", response_class=HTMLResponse)
async def people_stub(request: Request):
    from app.main import templates
    return templates.TemplateResponse("pages/people_stub.html", {
        "request": request,
        "title": "People",
    })
