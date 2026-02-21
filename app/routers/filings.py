"""Filings router — browse, filter, detail, PDF serve."""

from pathlib import Path

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select, func, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db import get_db
from app.models import Filing, Filer, Transaction
from app.config import PDF_STORAGE_PATH

router = APIRouter(tags=["filings"])

PAGE_SIZE = 24


@router.get("/filings", response_class=HTMLResponse)
async def filings_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Full filings browser page with filter controls."""
    from app.main import templates

    # Get distinct form types for filter dropdown
    form_types_q = await db.execute(
        select(Filing.form_type).distinct().order_by(Filing.form_type)
    )
    form_types = [r for r in form_types_q.scalars().all() if r]

    # Get filers for filter dropdown
    filers_q = await db.execute(
        select(Filer.filer_id, Filer.name).order_by(Filer.name)
    )
    filers = filers_q.all()

    return templates.TemplateResponse("pages/filings.html", {
        "request": request,
        "title": "Filings",
        "form_types": form_types,
        "filers": filers,
    })


@router.get("/filings/list", response_class=HTMLResponse)
async def filings_list(
    request: Request,
    page: int = Query(1, ge=1),
    form_type: str = Query("", alias="form_type"),
    filer_id: str = Query("", alias="filer_id"),
    date_from: str = Query("", alias="date_from"),
    date_to: str = Query("", alias="date_to"),
    search: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """HTMX partial — filing cards with infinite scroll."""
    from app.main import templates

    q = select(Filing).options(joinedload(Filing.filer))

    # Apply filters
    if form_type:
        q = q.where(Filing.form_type == form_type)
    if filer_id:
        q = q.where(Filing.filer_id == filer_id)
    if date_from:
        q = q.where(Filing.filing_date >= date_from)
    if date_to:
        q = q.where(Filing.filing_date <= date_to + " 23:59:59")
    if search:
        q = q.join(Filer).where(
            or_(
                Filer.name.ilike(f"%{search}%"),
                Filing.form_type.ilike(f"%{search}%"),
            )
        )

    # Count total
    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # Paginate
    q = q.order_by(desc(Filing.filing_date)).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    result = await db.execute(q)
    filings = result.unique().scalars().all()

    has_more = page * PAGE_SIZE < total

    return templates.TemplateResponse("components/filing_cards.html", {
        "request": request,
        "filings": filings,
        "page": page,
        "has_more": has_more,
        "total": total,
        "form_type": form_type,
        "filer_id": filer_id,
        "date_from": date_from,
        "date_to": date_to,
        "search": search,
    })


@router.get("/filings/{filing_id}", response_class=HTMLResponse)
async def filing_detail(
    request: Request,
    filing_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Filing detail page with transaction table + amendment chain."""
    from app.main import templates

    result = await db.execute(
        select(Filing).options(joinedload(Filing.filer)).where(Filing.filing_id == filing_id)
    )
    filing = result.unique().scalars().first()
    if not filing:
        return templates.TemplateResponse("errors/404.html", {
            "request": request, "title": "Filing Not Found",
        }, status_code=404)

    # Transaction counts and totals
    txn_stats = await db.execute(
        select(
            func.count(Transaction.transaction_id),
            func.coalesce(func.sum(Transaction.amount), 0),
        ).where(Transaction.filing_id == filing_id)
    )
    txn_count, txn_total = txn_stats.one()

    # Amendment chain
    amendments = []
    if filing.netfile_filing_id:
        amend_q = await db.execute(
            select(Filing)
            .where(Filing.filer_id == filing.filer_id)
            .where(Filing.form_type == filing.form_type)
            .where(Filing.period_start == filing.period_start)
            .where(Filing.period_end == filing.period_end)
            .order_by(Filing.amendment_seq)
        )
        amendments = amend_q.scalars().all()

    return templates.TemplateResponse("pages/filing_detail.html", {
        "request": request,
        "title": f"{filing.form_type} - {filing.filer.name if filing.filer else 'Unknown'}",
        "filing": filing,
        "txn_count": txn_count,
        "txn_total": txn_total,
        "amendments": amendments,
    })


@router.get("/filings/{filing_id}/transactions", response_class=HTMLResponse)
async def filing_transactions(
    request: Request,
    filing_id: str,
    db: AsyncSession = Depends(get_db),
):
    """HTMX partial — transaction rows for a filing."""
    from app.main import templates

    result = await db.execute(
        select(Transaction)
        .where(Transaction.filing_id == filing_id)
        .order_by(Transaction.transaction_date.desc(), Transaction.amount.desc())
    )
    transactions = result.scalars().all()

    return templates.TemplateResponse("components/transaction_detail_rows.html", {
        "request": request,
        "transactions": transactions,
    })


@router.get("/pdfs/{netfile_filing_id}.pdf")
async def serve_pdf(netfile_filing_id: str):
    """Serve locally stored PDF files."""
    pdf_path = PDF_STORAGE_PATH / f"{netfile_filing_id}.pdf"
    if not pdf_path.exists():
        return HTMLResponse("<p>PDF not found</p>", status_code=404)
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"{netfile_filing_id}.pdf",
    )
