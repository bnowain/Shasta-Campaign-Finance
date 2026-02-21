"""Global search router — HTMX dropdown results."""

from fastapi import APIRouter, Request, Query, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services.search_indexer import search_fts

router = APIRouter(tags=["search"])


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """HTMX partial — global search dropdown."""
    from app.main import templates

    if not q or len(q) < 2:
        return HTMLResponse("")

    results = await search_fts(q, limit=10, db=db)

    return templates.TemplateResponse("components/search_results.html", {
        "request": request,
        "results": results,
        "query": q,
    })
