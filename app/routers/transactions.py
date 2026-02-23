"""Transactions router — search, filter, CSV export."""

import csv
import io
from datetime import date

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select, func, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db import get_db
from app.models import Transaction, Filing, Filer, TransactionPerson

router = APIRouter(tags=["transactions"])

PAGE_SIZE = 50


def _apply_filters(q, search, schedule, filer_id, amount_min, amount_max, date_from, date_to):
    """Apply common filters to a transaction query."""
    if search:
        q = q.where(
            or_(
                Transaction.entity_name.ilike(f"%{search}%"),
                Transaction.description.ilike(f"%{search}%"),
                Transaction.employer.ilike(f"%{search}%"),
                Transaction.occupation.ilike(f"%{search}%"),
            )
        )
    if schedule:
        q = q.where(Transaction.schedule == schedule)
    if filer_id:
        q = q.where(Filing.filer_id == filer_id)
    if amount_min:
        try:
            q = q.where(Transaction.amount >= float(amount_min))
        except ValueError:
            pass
    if amount_max:
        try:
            q = q.where(Transaction.amount <= float(amount_max))
        except ValueError:
            pass
    if date_from:
        q = q.where(Transaction.transaction_date >= date_from)
    if date_to:
        q = q.where(Transaction.transaction_date <= date_to)
    return q


@router.get("/transactions", response_class=HTMLResponse)
async def transactions_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Transaction search page with filters."""
    from app.main import templates

    # Get distinct schedules
    sched_q = await db.execute(
        select(Transaction.schedule).distinct().order_by(Transaction.schedule)
    )
    schedules = [s for s in sched_q.scalars().all() if s]

    # Get filers for dropdown
    filers_q = await db.execute(
        select(Filer.filer_id, Filer.name).order_by(Filer.name)
    )
    filers = filers_q.all()

    return templates.TemplateResponse("pages/transactions.html", {
        "request": request,
        "title": "Transactions",
        "schedules": schedules,
        "filers": filers,
    })


@router.get("/transactions/list", response_class=HTMLResponse)
async def transactions_list(
    request: Request,
    page: int = Query(1, ge=1),
    search: str = Query(""),
    schedule: str = Query(""),
    filer_id: str = Query("", alias="filer_id"),
    amount_min: str = Query("", alias="amount_min"),
    amount_max: str = Query("", alias="amount_max"),
    date_from: str = Query("", alias="date_from"),
    date_to: str = Query("", alias="date_to"),
    db: AsyncSession = Depends(get_db),
):
    """HTMX partial — transaction rows with infinite scroll."""
    from app.main import templates

    q = select(Transaction).join(Filing)

    q = _apply_filters(q, search, schedule, filer_id, amount_min, amount_max, date_from, date_to)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    q = q.order_by(desc(Transaction.transaction_date), desc(Transaction.amount))
    q = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)

    # Re-add joinedload after filtering (need fresh select for eager load)
    result = await db.execute(q)
    transactions = result.scalars().all()

    # Load filing+filer and person links for display
    for txn in transactions:
        await db.refresh(txn, ["filing"])
        if txn.filing:
            await db.refresh(txn.filing, ["filer"])
        # Load person links with person relationship
        await db.refresh(txn, ["person_links"])
        for pl in txn.person_links:
            await db.refresh(pl, ["person"])

    has_more = page * PAGE_SIZE < total

    return templates.TemplateResponse("components/transaction_rows.html", {
        "request": request,
        "transactions": transactions,
        "page": page,
        "has_more": has_more,
        "total": total,
        "search": search,
        "schedule": schedule,
        "filer_id": filer_id,
        "amount_min": amount_min,
        "amount_max": amount_max,
        "date_from": date_from,
        "date_to": date_to,
    })


@router.get("/transactions/export")
async def transactions_export(
    search: str = Query(""),
    schedule: str = Query(""),
    filer_id: str = Query("", alias="filer_id"),
    amount_min: str = Query("", alias="amount_min"),
    amount_max: str = Query("", alias="amount_max"),
    date_from: str = Query("", alias="date_from"),
    date_to: str = Query("", alias="date_to"),
    db: AsyncSession = Depends(get_db),
):
    """CSV export of transactions with current filters."""
    q = select(Transaction).join(Filing)
    q = _apply_filters(q, search, schedule, filer_id, amount_min, amount_max, date_from, date_to)
    q = q.order_by(desc(Transaction.transaction_date))

    result = await db.execute(q)
    transactions = result.scalars().all()

    # Load related data
    for txn in transactions:
        await db.refresh(txn, ["filing"])
        if txn.filing:
            await db.refresh(txn.filing, ["filer"])

    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Date", "Schedule", "Name", "City", "State",
            "Employer", "Occupation", "Amount", "Cumulative",
            "Description", "Filer", "Form Type",
        ])
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        for txn in transactions:
            filer_name = txn.filing.filer.name if txn.filing and txn.filing.filer else ""
            form_type = txn.filing.form_type if txn.filing else ""
            writer.writerow([
                txn.transaction_date.isoformat() if txn.transaction_date else "",
                txn.schedule or "",
                txn.entity_name or "",
                txn.city or "",
                txn.state or "",
                txn.employer or "",
                txn.occupation or "",
                f"{txn.amount:.2f}",
                f"{txn.cumulative_amount:.2f}" if txn.cumulative_amount else "",
                txn.description or "",
                filer_name,
                form_type,
            ])
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    today = date.today().isoformat()
    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=transactions_{today}.csv"},
    )
