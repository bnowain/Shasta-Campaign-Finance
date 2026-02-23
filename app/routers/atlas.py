"""Atlas hub integration endpoints — spoke registration and people search."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import APP_PORT, APP_HOST, ATLAS_SPOKE_NAME, NETFILE_AID
from app.db import get_db
from app.models import Person, FilerPerson, TransactionPerson

router = APIRouter(prefix="/api", tags=["atlas"])


@router.post("/atlas/register")
async def register_spoke():
    """Return spoke registration info for Atlas hub."""
    return {
        "name": ATLAS_SPOKE_NAME,
        "url": f"http://{APP_HOST}:{APP_PORT}",
        "port": APP_PORT,
        "agency": NETFILE_AID,
        "type": "campaign_finance",
        "endpoints": {
            "health": "/api/health",
            "people_search": "/api/people/search",
        },
    }


@router.get("/people/search")
async def search_people(
    q: str = Query("", min_length=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Atlas People search endpoint — query Person by name or aliases."""
    if not q or not q.strip():
        return {"source": ATLAS_SPOKE_NAME, "query": q, "results": [], "total": 0}

    pattern = f"%{q}%"
    people = (await db.execute(
        select(Person)
        .where(
            or_(
                Person.canonical_name.ilike(pattern),
                Person.aliases.ilike(pattern),
            )
        )
        .order_by(Person.canonical_name)
        .limit(limit)
    )).scalars().all()

    results = []
    for p in people:
        filer_count = (await db.execute(
            select(func.count(FilerPerson.id)).where(FilerPerson.person_id == p.person_id)
        )).scalar() or 0
        txn_count = (await db.execute(
            select(func.count(TransactionPerson.id)).where(TransactionPerson.person_id == p.person_id)
        )).scalar() or 0

        results.append({
            "person_id": p.person_id,
            "canonical_name": p.canonical_name,
            "entity_type": p.entity_type,
            "appearances": {
                "as_filer": filer_count,
                "as_transaction_party": txn_count,
            },
            "detail_url": f"http://{APP_HOST}:{APP_PORT}/people/{p.person_id}",
        })

    return {
        "source": ATLAS_SPOKE_NAME,
        "query": q,
        "results": results,
        "total": len(results),
    }
