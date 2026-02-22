"""Filers router — directory, search, detail."""

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db import get_db
from app.models import Filer, Filing, Transaction, ElectionCandidate

router = APIRouter(tags=["filers"])

PAGE_SIZE = 30


@router.get("/filers", response_class=HTMLResponse)
async def filers_page(request: Request):
    """Filer directory page with search and filters."""
    from app.main import templates
    return templates.TemplateResponse("pages/filers.html", {
        "request": request,
        "title": "Filers",
    })


@router.get("/filers/list", response_class=HTMLResponse)
async def filers_list(
    request: Request,
    page: int = Query(1, ge=1),
    search: str = Query(""),
    filer_type: str = Query("", alias="filer_type"),
    status: str = Query("", alias="status"),
    db: AsyncSession = Depends(get_db),
):
    """HTMX partial — filer rows with infinite scroll."""
    from app.main import templates

    q = select(Filer)

    if search:
        q = q.where(
            or_(
                Filer.name.ilike(f"%{search}%"),
                Filer.local_filer_id.ilike(f"%{search}%"),
            )
        )
    if filer_type:
        q = q.where(Filer.filer_type == filer_type)
    if status:
        q = q.where(Filer.status == status)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    q = q.order_by(Filer.name).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    result = await db.execute(q)
    filers = result.scalars().all()

    has_more = page * PAGE_SIZE < total

    return templates.TemplateResponse("components/filer_rows.html", {
        "request": request,
        "filers": filers,
        "page": page,
        "has_more": has_more,
        "total": total,
        "search": search,
        "filer_type": filer_type,
        "status": status,
    })


@router.get("/filers/{filer_id}", response_class=HTMLResponse)
async def filer_detail(
    request: Request,
    filer_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Filer detail page — filing history + financial summary."""
    from app.main import templates

    filer = (await db.execute(select(Filer).where(Filer.filer_id == filer_id))).scalars().first()
    if not filer:
        return templates.TemplateResponse("errors/404.html", {
            "request": request, "title": "Filer Not Found",
        }, status_code=404)

    # Filing history
    filings_result = await db.execute(
        select(Filing)
        .where(Filing.filer_id == filer_id)
        .order_by(desc(Filing.filing_date))
    )
    filings = filings_result.scalars().all()

    # Financial summary via transactions through filings
    filing_ids_q = select(Filing.filing_id).where(Filing.filer_id == filer_id)
    stats = await db.execute(
        select(
            func.count(Transaction.transaction_id),
            func.coalesce(func.sum(Transaction.amount), 0),
        ).where(Transaction.filing_id.in_(filing_ids_q))
    )
    txn_count, txn_total = stats.one()

    # Contributions (Schedule A, C) vs Expenditures (Schedule E) vs other
    contributions_result = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.filing_id.in_(filing_ids_q),
            Transaction.schedule.in_(["A", "C"]),
        )
    )
    contributions_total = contributions_result.scalar() or 0

    expenditures_result = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.filing_id.in_(filing_ids_q),
            Transaction.schedule == "E",
        )
    )
    expenditures_total = expenditures_result.scalar() or 0

    # Election history
    election_links_result = await db.execute(
        select(ElectionCandidate)
        .options(joinedload(ElectionCandidate.election))
        .where(ElectionCandidate.filer_id == filer_id)
        .order_by(desc(ElectionCandidate.created_at))
    )
    election_links = election_links_result.unique().scalars().all()

    return templates.TemplateResponse("pages/filer_detail.html", {
        "request": request,
        "title": filer.name,
        "filer": filer,
        "filings": filings,
        "txn_count": txn_count,
        "txn_total": txn_total,
        "contributions_total": contributions_total,
        "expenditures_total": expenditures_total,
        "election_links": election_links,
    })
