"""Elections router — list, detail, data source serving."""

from pathlib import Path

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, FileResponse
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db import get_db
from app.models import Election, ElectionCandidate, Filer

router = APIRouter(tags=["elections"])

PAGE_SIZE = 20
BASE_DIR = Path(__file__).resolve().parent.parent.parent
ELECTIONS_PDF_DIR = BASE_DIR / "data" / "elections" / "pdfs"
CLARITY_DIR = BASE_DIR / "data" / "elections" / "clarity"

# MIME types for serving Clarity files
MIME_MAP = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".html": "text/html",
    ".xls": "application/vnd.ms-excel",
}


@router.get("/elections", response_class=HTMLResponse)
async def elections_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Elections list page."""
    from app.main import templates

    # Get available years and types for filter dropdowns
    years_result = await db.execute(
        select(Election.year).distinct().order_by(desc(Election.year))
    )
    years = [r[0] for r in years_result.all()]

    types_result = await db.execute(
        select(Election.election_type).distinct().where(Election.election_type.isnot(None))
    )
    election_types = sorted([r[0] for r in types_result.all()])

    return templates.TemplateResponse("pages/elections.html", {
        "request": request,
        "title": "Elections",
        "years": years,
        "election_types": election_types,
    })


@router.get("/elections/list", response_class=HTMLResponse)
async def elections_list(
    request: Request,
    page: int = Query(1, ge=1),
    year: str = Query(""),
    election_type: str = Query("", alias="election_type"),
    search: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """HTMX partial — election rows with infinite scroll."""
    from app.main import templates

    q = select(Election)

    if year:
        q = q.where(Election.year == int(year))
    if election_type:
        q = q.where(Election.election_type == election_type)
    if search:
        q = q.where(Election.name.ilike(f"%{search}%"))

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    q = q.order_by(desc(Election.date)).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    result = await db.execute(q)
    elections = result.scalars().all()

    # Get candidate counts per election
    candidate_counts = {}
    if elections:
        eids = [e.election_id for e in elections]
        counts_result = await db.execute(
            select(
                ElectionCandidate.election_id,
                func.count(ElectionCandidate.id),
            )
            .where(ElectionCandidate.election_id.in_(eids))
            .group_by(ElectionCandidate.election_id)
        )
        candidate_counts = dict(counts_result.all())

    has_more = page * PAGE_SIZE < total

    return templates.TemplateResponse("components/election_rows.html", {
        "request": request,
        "elections": elections,
        "candidate_counts": candidate_counts,
        "page": page,
        "has_more": has_more,
        "total": total,
        "year": year,
        "election_type": election_type,
        "search": search,
    })


@router.get("/elections/{election_id}", response_class=HTMLResponse)
async def election_detail(
    request: Request,
    election_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Election detail page — candidates grouped by office."""
    from app.main import templates

    election = (await db.execute(
        select(Election).where(Election.election_id == election_id)
    )).scalars().first()

    if not election:
        return templates.TemplateResponse("errors/404.html", {
            "request": request, "title": "Election Not Found",
        }, status_code=404)

    # Get candidates with filer info
    candidates_result = await db.execute(
        select(ElectionCandidate)
        .options(joinedload(ElectionCandidate.filer))
        .where(ElectionCandidate.election_id == election_id)
        .order_by(ElectionCandidate.office_sought, ElectionCandidate.finish_position)
    )
    candidates = candidates_result.unique().scalars().all()

    # Group candidates by office
    offices = {}
    measures = []
    for c in candidates:
        if c.is_measure:
            measures.append(c)
        else:
            office = c.office_sought or "Unknown Office"
            offices.setdefault(office, []).append(c)

    # Sort candidates within each office: winners first, then by votes desc
    for office_candidates in offices.values():
        office_candidates.sort(key=lambda c: (
            not (c.is_winner or False),
            -(c.votes_received or 0),
        ))

    # Stats
    total_candidates = len([c for c in candidates if not c.is_measure])
    total_races = len(offices)
    has_results = any(c.votes_received for c in candidates)

    # Find source files from Clarity downloads
    source_files = _find_election_source_files(election)

    return templates.TemplateResponse("pages/election_detail.html", {
        "request": request,
        "title": election.name,
        "election": election,
        "offices": offices,
        "measures": measures,
        "total_candidates": total_candidates,
        "total_races": total_races,
        "has_results": has_results,
        "source_files": source_files,
    })


@router.get("/elections/pdf/{filename:path}")
async def serve_election_pdf(filename: str):
    """Serve a locally stored election result PDF."""
    path = ELECTIONS_PDF_DIR / filename
    if not path.exists() or not path.is_file():
        return HTMLResponse("PDF not found", status_code=404)
    return FileResponse(path, media_type="application/pdf")


@router.get("/elections/files/{election_slug}/{filename:path}")
async def serve_election_file(election_slug: str, filename: str):
    """Serve a downloaded Clarity Elections file (PDF, Excel, CSV, HTML)."""
    # Prevent path traversal
    if ".." in election_slug or ".." in filename:
        return HTMLResponse("Invalid path", status_code=400)

    path = CLARITY_DIR / election_slug / filename
    if not path.exists() or not path.is_file():
        return HTMLResponse("File not found", status_code=404)

    # Resolve to ensure we're still within CLARITY_DIR
    resolved = path.resolve()
    if not str(resolved).startswith(str(CLARITY_DIR.resolve())):
        return HTMLResponse("Invalid path", status_code=400)

    media_type = MIME_MAP.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(resolved, media_type=media_type, filename=filename)


def _find_election_source_files(election) -> list[dict]:
    """Find Clarity source files that belong to this election.

    Matches by year and date components in the directory slug.
    """
    if not CLARITY_DIR.exists():
        return []

    date = election.date
    if not date:
        return []

    # Build expected slug components
    year_str = str(date.year)
    mmdd = f"{date.month:02d}{date.day:02d}"

    files = []
    for subdir in sorted(CLARITY_DIR.iterdir()):
        if not subdir.is_dir():
            continue
        slug = subdir.name
        # Match by year + month/day in slug
        if year_str in slug and mmdd in slug:
            for f in sorted(subdir.iterdir()):
                if f.is_file() and f.stat().st_size > 0:
                    files.append({
                        "slug": slug,
                        "filename": f.name,
                        "ext": f.suffix.lower(),
                        "size": f.stat().st_size,
                    })
            if files:
                return files

    # Fallback: match by year only (for elections without exact date match)
    for subdir in sorted(CLARITY_DIR.iterdir()):
        if not subdir.is_dir():
            continue
        if year_str in subdir.name:
            for f in sorted(subdir.iterdir()):
                if f.is_file() and f.stat().st_size > 0:
                    files.append({
                        "slug": subdir.name,
                        "filename": f.name,
                        "ext": f.suffix.lower(),
                        "size": f.stat().st_size,
                    })
            if files:
                return files

    return files
