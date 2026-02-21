"""Atlas hub integration endpoints — spoke registration and people search."""

from fastapi import APIRouter

from app.config import APP_PORT, APP_HOST, ATLAS_SPOKE_NAME, NETFILE_AID

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
async def search_people(q: str = "", limit: int = 20):
    """Atlas People search endpoint. Stub for Phase 6."""
    return {
        "query": q,
        "limit": limit,
        "results": [],
        "total": 0,
        "note": "People search not yet implemented — Phase 6",
    }
