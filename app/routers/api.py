"""JSON API endpoints for Atlas spoke integration.

These endpoints return JSON (not HTML) so Atlas can call them via tool handlers.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db import get_db
from app.models import Filer, Filing, Transaction, Person, Election, ElectionCandidate

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/stats")
async def stats(db: AsyncSession = Depends(get_db)):
    """Dashboard statistics as JSON."""
    filer_count = (await db.execute(select(func.count(Filer.filer_id)))).scalar() or 0
    filing_count = (await db.execute(select(func.count(Filing.filing_id)))).scalar() or 0
    txn_count = (await db.execute(select(func.count(Transaction.transaction_id)))).scalar() or 0
    person_count = (await db.execute(select(func.count(Person.person_id)))).scalar() or 0
    election_count = (await db.execute(select(func.count(Election.election_id)))).scalar() or 0

    contributions = (await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0))
        .where(Transaction.schedule.in_(["A", "C"]))
    )).scalar() or 0

    expenditures = (await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0))
        .where(Transaction.schedule == "E")
    )).scalar() or 0

    return {
        "filers": filer_count,
        "filings": filing_count,
        "transactions": txn_count,
        "people": person_count,
        "elections": election_count,
        "total_contributions": round(float(contributions), 2),
        "total_expenditures": round(float(expenditures), 2),
    }


@router.get("/filers")
async def list_filers(
    search: str = Query(""),
    filer_type: str = Query(""),
    status: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """List/search filers as JSON."""
    q = select(Filer)
    if search:
        q = q.where(or_(
            Filer.name.ilike(f"%{search}%"),
            Filer.local_filer_id.ilike(f"%{search}%"),
        ))
    if filer_type:
        q = q.where(Filer.filer_type == filer_type)
    if status:
        q = q.where(Filer.status == status)

    q = q.order_by(Filer.name).limit(limit)
    result = await db.execute(q)
    filers = result.scalars().all()

    return [
        {
            "filer_id": f.filer_id,
            "name": f.name,
            "filer_type": f.filer_type,
            "status": f.status,
            "office": f.office,
            "jurisdiction": f.jurisdiction,
            "local_filer_id": f.local_filer_id,
            "first_filing": str(f.first_filing) if f.first_filing else None,
            "last_filing": str(f.last_filing) if f.last_filing else None,
        }
        for f in filers
    ]


@router.get("/filers/{filer_id}")
async def get_filer(filer_id: str, db: AsyncSession = Depends(get_db)):
    """Get filer detail as JSON."""
    filer = (await db.execute(
        select(Filer).where(Filer.filer_id == filer_id)
    )).scalar_one_or_none()
    if not filer:
        return {"error": "Filer not found"}

    filing_count = (await db.execute(
        select(func.count(Filing.filing_id)).where(Filing.filer_id == filer_id)
    )).scalar() or 0

    contributions = (await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0))
        .join(Filing, Transaction.filing_id == Filing.filing_id)
        .where(Filing.filer_id == filer_id)
        .where(Transaction.schedule.in_(["A", "C"]))
    )).scalar() or 0

    expenditures = (await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0))
        .join(Filing, Transaction.filing_id == Filing.filing_id)
        .where(Filing.filer_id == filer_id)
        .where(Transaction.schedule == "E")
    )).scalar() or 0

    return {
        "filer_id": filer.filer_id,
        "name": filer.name,
        "filer_type": filer.filer_type,
        "status": filer.status,
        "office": filer.office,
        "jurisdiction": filer.jurisdiction,
        "local_filer_id": filer.local_filer_id,
        "first_filing": str(filer.first_filing) if filer.first_filing else None,
        "last_filing": str(filer.last_filing) if filer.last_filing else None,
        "filing_count": filing_count,
        "total_contributions": round(float(contributions), 2),
        "total_expenditures": round(float(expenditures), 2),
    }


@router.get("/filings")
async def list_filings(
    search: str = Query(""),
    form_type: str = Query(""),
    filer_id: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """List/search filings as JSON."""
    q = select(Filing).options(joinedload(Filing.filer))

    if search:
        q = q.where(or_(
            Filing.form_name.ilike(f"%{search}%"),
            Filing.form_type.ilike(f"%{search}%"),
        ))
    if form_type:
        q = q.where(Filing.form_type == form_type)
    if filer_id:
        q = q.where(Filing.filer_id == filer_id)
    if date_from:
        q = q.where(Filing.filing_date >= date_from)
    if date_to:
        q = q.where(Filing.filing_date <= date_to)

    q = q.order_by(desc(Filing.filing_date)).limit(limit)
    result = await db.execute(q)
    filings = result.unique().scalars().all()

    return [
        {
            "filing_id": f.filing_id,
            "filer_name": f.filer.name if f.filer else None,
            "filer_id": f.filer_id,
            "form_type": f.form_type,
            "form_name": f.form_name,
            "filing_date": str(f.filing_date) if f.filing_date else None,
            "period_start": str(f.period_start) if f.period_start else None,
            "period_end": str(f.period_end) if f.period_end else None,
            "amendment_seq": f.amendment_seq,
        }
        for f in filings
    ]


@router.get("/transactions")
async def list_transactions(
    search: str = Query(""),
    schedule: str = Query(""),
    filer_id: str = Query(""),
    amount_min: str = Query(""),
    amount_max: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """List/search transactions as JSON."""
    q = select(Transaction).join(Filing, Transaction.filing_id == Filing.filing_id)

    if search:
        q = q.where(or_(
            Transaction.entity_name.ilike(f"%{search}%"),
            Transaction.description.ilike(f"%{search}%"),
            Transaction.employer.ilike(f"%{search}%"),
            Transaction.occupation.ilike(f"%{search}%"),
        ))
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

    q = q.order_by(desc(Transaction.transaction_date)).limit(limit)
    result = await db.execute(q)
    txns = result.scalars().all()

    return [
        {
            "transaction_id": t.transaction_id,
            "filing_id": t.filing_id,
            "schedule": t.schedule,
            "transaction_type": t.transaction_type,
            "entity_name": t.entity_name,
            "entity_type": t.entity_type,
            "first_name": t.first_name,
            "last_name": t.last_name,
            "city": t.city,
            "state": t.state,
            "employer": t.employer,
            "occupation": t.occupation,
            "amount": float(t.amount) if t.amount else 0,
            "cumulative_amount": float(t.cumulative_amount) if t.cumulative_amount else None,
            "transaction_date": str(t.transaction_date) if t.transaction_date else None,
            "description": t.description,
        }
        for t in txns
    ]


@router.get("/elections")
async def list_elections(
    year: str = Query(""),
    search: str = Query(""),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """List elections as JSON."""
    q = select(Election)
    if year:
        try:
            q = q.where(Election.year == int(year))
        except ValueError:
            pass
    if search:
        q = q.where(Election.name.ilike(f"%{search}%"))

    q = q.order_by(desc(Election.date)).limit(limit)
    result = await db.execute(q)
    elections = result.scalars().all()

    return [
        {
            "election_id": e.election_id,
            "name": e.name,
            "date": str(e.date) if e.date else None,
            "election_type": e.election_type,
            "year": e.year,
            "turnout_percentage": e.turnout_percentage,
            "results_certified": e.results_certified,
        }
        for e in elections
    ]
