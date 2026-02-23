"""People router — directory, detail, CRUD, review queue."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, func, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db import get_db
from app.models import (
    Person, Filer, Transaction, Filing,
    FilerPerson, TransactionPerson,
)

router = APIRouter(tags=["people"])

PAGE_SIZE = 30


@router.get("/people", response_class=HTMLResponse)
async def people_page(request: Request):
    """People directory page with filters."""
    from app.main import templates
    return templates.TemplateResponse("pages/people.html", {
        "request": request,
        "title": "People",
    })


@router.get("/people/list", response_class=HTMLResponse)
async def people_list(
    request: Request,
    page: int = Query(1, ge=1),
    search: str = Query(""),
    entity_type: str = Query("", alias="entity_type"),
    db: AsyncSession = Depends(get_db),
):
    """HTMX partial — person rows with infinite scroll."""
    from app.main import templates

    q = select(Person)

    if search:
        pattern = f"%{search}%"
        q = q.where(
            or_(
                Person.canonical_name.ilike(pattern),
                Person.aliases.ilike(pattern),
            )
        )
    if entity_type:
        q = q.where(Person.entity_type == entity_type)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    q = q.order_by(Person.canonical_name).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    people = (await db.execute(q)).scalars().all()

    # Get link counts for each person
    person_ids = [p.person_id for p in people]
    filer_counts = {}
    txn_counts = {}

    if person_ids:
        fc_result = await db.execute(
            select(FilerPerson.person_id, func.count(FilerPerson.id))
            .where(FilerPerson.person_id.in_(person_ids))
            .group_by(FilerPerson.person_id)
        )
        filer_counts = dict(fc_result.all())

        tc_result = await db.execute(
            select(TransactionPerson.person_id, func.count(TransactionPerson.id))
            .where(TransactionPerson.person_id.in_(person_ids))
            .group_by(TransactionPerson.person_id)
        )
        txn_counts = dict(tc_result.all())

    has_more = page * PAGE_SIZE < total

    return templates.TemplateResponse("components/person_rows.html", {
        "request": request,
        "people": people,
        "filer_counts": filer_counts,
        "txn_counts": txn_counts,
        "page": page,
        "has_more": has_more,
        "total": total,
        "search": search,
        "entity_type": entity_type,
    })


@router.get("/people/review", response_class=HTMLResponse)
async def people_review(request: Request, db: AsyncSession = Depends(get_db)):
    """Review queue for medium-confidence matches."""
    from app.main import templates

    # Transaction links needing review
    txn_reviews = (await db.execute(
        select(TransactionPerson)
        .options(joinedload(TransactionPerson.person))
        .options(joinedload(TransactionPerson.transaction))
        .where(TransactionPerson.needs_review == True)
        .order_by(TransactionPerson.match_confidence.desc())
        .limit(100)
    )).unique().scalars().all()

    # Filer links needing review
    filer_reviews = (await db.execute(
        select(FilerPerson)
        .options(joinedload(FilerPerson.person))
        .options(joinedload(FilerPerson.filer))
        .where(FilerPerson.needs_review == True)
        .order_by(FilerPerson.match_confidence.desc())
        .limit(100)
    )).unique().scalars().all()

    # Count totals
    txn_review_count = (await db.execute(
        select(func.count(TransactionPerson.id)).where(TransactionPerson.needs_review == True)
    )).scalar() or 0

    filer_review_count = (await db.execute(
        select(func.count(FilerPerson.id)).where(FilerPerson.needs_review == True)
    )).scalar() or 0

    return templates.TemplateResponse("pages/people_review.html", {
        "request": request,
        "title": "Review Queue",
        "txn_reviews": txn_reviews,
        "filer_reviews": filer_reviews,
        "txn_review_count": txn_review_count,
        "filer_review_count": filer_review_count,
    })


@router.post("/people/review/{link_type}/{link_id}/resolve", response_class=HTMLResponse)
async def resolve_review(
    link_type: str,
    link_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Approve or reject a flagged link."""
    from app.main import templates

    form = await request.form()
    action = form.get("action", "approve")

    if link_type == "transaction":
        link = (await db.execute(
            select(TransactionPerson).where(TransactionPerson.id == link_id)
        )).scalars().first()
        if link:
            if action == "approve":
                link.needs_review = False
            else:
                await db.delete(link)
            await db.commit()

    elif link_type == "filer":
        link = (await db.execute(
            select(FilerPerson).where(FilerPerson.id == link_id)
        )).scalars().first()
        if link:
            if action == "approve":
                link.needs_review = False
            else:
                await db.delete(link)
            await db.commit()

    # Return empty row (removed from list)
    return HTMLResponse("")


@router.get("/people/search-typeahead")
async def people_typeahead(
    q: str = Query("", min_length=1),
    db: AsyncSession = Depends(get_db),
):
    """JSON results for inline tagging type-ahead (limit 10)."""
    if not q:
        return JSONResponse([])

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
        .limit(10)
    )).scalars().all()

    return JSONResponse([
        {
            "person_id": p.person_id,
            "canonical_name": p.canonical_name,
            "entity_type": p.entity_type or "unknown",
        }
        for p in people
    ])


@router.get("/people/merge-preview", response_class=HTMLResponse)
async def merge_preview(
    request: Request,
    ids: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """HTMX merge preview with canonical selection."""
    from app.main import templates

    person_ids = [i.strip() for i in ids.split(",") if i.strip()]
    if len(person_ids) < 2:
        return HTMLResponse("<p class='text-muted'>Select at least 2 people to merge.</p>")

    people = (await db.execute(
        select(Person).where(Person.person_id.in_(person_ids))
    )).scalars().all()

    if len(people) < 2:
        return HTMLResponse("<p class='text-muted'>People not found.</p>")

    merge_data = []
    for p in people:
        filer_count = (await db.execute(
            select(func.count(FilerPerson.id)).where(FilerPerson.person_id == p.person_id)
        )).scalar() or 0
        txn_count = (await db.execute(
            select(func.count(TransactionPerson.id)).where(TransactionPerson.person_id == p.person_id)
        )).scalar() or 0
        merge_data.append({
            "person": p,
            "filer_count": filer_count,
            "txn_count": txn_count,
        })

    return templates.TemplateResponse("components/person_merge_preview.html", {
        "request": request,
        "merge_data": merge_data,
    })


@router.get("/people/tag-search", response_class=HTMLResponse)
async def tag_search(
    request: Request,
    transaction_id: str = Query(""),
    q: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Render inline type-ahead search dropdown for person tagging."""
    from app.main import templates

    results = []
    if q and len(q) >= 1:
        pattern = f"%{q}%"
        results = (await db.execute(
            select(Person)
            .where(
                or_(
                    Person.canonical_name.ilike(pattern),
                    Person.aliases.ilike(pattern),
                )
            )
            .order_by(Person.canonical_name)
            .limit(10)
        )).scalars().all()

    return templates.TemplateResponse("components/person_tag_search.html", {
        "request": request,
        "results": results,
        "transaction_id": transaction_id,
        "q": q,
    })


@router.get("/people/{person_id}", response_class=HTMLResponse)
async def person_detail(
    request: Request,
    person_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Person detail page — filer links, transaction links, financials."""
    from app.main import templates

    person = (await db.execute(
        select(Person).where(Person.person_id == person_id)
    )).scalars().first()

    if not person:
        return templates.TemplateResponse("errors/404.html", {
            "request": request, "title": "Person Not Found",
        }, status_code=404)

    # Filer associations
    filer_links = (await db.execute(
        select(FilerPerson)
        .options(joinedload(FilerPerson.filer))
        .where(FilerPerson.person_id == person_id)
        .order_by(FilerPerson.created_at)
    )).unique().scalars().all()

    # Transaction appearances (limited)
    txn_links = (await db.execute(
        select(TransactionPerson)
        .options(joinedload(TransactionPerson.transaction))
        .where(TransactionPerson.person_id == person_id)
        .order_by(desc(TransactionPerson.created_at))
        .limit(50)
    )).unique().scalars().all()

    # Load filing → filer for transaction display
    for tl in txn_links:
        if tl.transaction:
            await db.refresh(tl.transaction, ["filing"])
            if tl.transaction.filing:
                await db.refresh(tl.transaction.filing, ["filer"])

    # Financial summary
    txn_person_ids = select(TransactionPerson.transaction_id).where(
        TransactionPerson.person_id == person_id
    )

    # Total as contributor (Schedule A, C)
    contributed = (await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.transaction_id.in_(txn_person_ids),
            Transaction.schedule.in_(["A", "C"]),
        )
    )).scalar() or 0

    # Total as payee (Schedule E)
    as_payee = (await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.transaction_id.in_(txn_person_ids),
            Transaction.schedule == "E",
        )
    )).scalar() or 0

    txn_total_count = (await db.execute(
        select(func.count(TransactionPerson.id)).where(
            TransactionPerson.person_id == person_id
        )
    )).scalar() or 0

    # Parse aliases
    aliases = []
    if person.aliases:
        try:
            aliases = json.loads(person.aliases) if isinstance(person.aliases, str) else person.aliases
        except (json.JSONDecodeError, TypeError):
            pass

    return templates.TemplateResponse("pages/person_detail.html", {
        "request": request,
        "title": person.canonical_name,
        "person": person,
        "filer_links": filer_links,
        "txn_links": txn_links,
        "contributed": contributed,
        "as_payee": as_payee,
        "txn_total_count": txn_total_count,
        "aliases": aliases,
    })


@router.post("/people/create", response_class=HTMLResponse)
async def create_person(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create a new Person from form."""
    from app.main import templates

    form = await request.form()
    name = str(form.get("canonical_name", "")).strip()
    entity_type = str(form.get("entity_type", "individual")).strip()
    notes = str(form.get("notes", "")).strip() or None

    if not name:
        return HTMLResponse("<p class='text-muted'>Name is required.</p>", status_code=400)

    # Check for duplicate
    existing = (await db.execute(
        select(Person).where(func.lower(Person.canonical_name) == name.lower())
    )).scalars().first()

    if existing:
        return HTMLResponse(
            f"<p class='text-muted'>Person '{name}' already exists.</p>",
            status_code=400,
        )

    person = Person(
        canonical_name=name,
        entity_type=entity_type,
        notes=notes,
    )
    db.add(person)
    await db.commit()

    # Return the create form area with a success message that auto-dismisses
    return HTMLResponse(
        f'<p class="tag tag-success">Created: {name}</p>'
        f'<script>setTimeout(function(){{ document.getElementById("create-result").innerHTML = ""; }}, 3000);</script>'
    )


@router.post("/people/{person_id}/edit", response_class=HTMLResponse)
async def edit_person(
    person_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Update Person fields."""
    from app.main import templates

    person = (await db.execute(
        select(Person).where(Person.person_id == person_id)
    )).scalars().first()

    if not person:
        return HTMLResponse("Not found", status_code=404)

    form = await request.form()
    name = str(form.get("canonical_name", "")).strip()
    entity_type = str(form.get("entity_type", "")).strip()
    notes = str(form.get("notes", "")).strip()

    if name:
        person.canonical_name = name
    if entity_type:
        person.entity_type = entity_type
    person.notes = notes or None

    await db.commit()

    # Redirect back to detail page
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/people/{person_id}", status_code=303)


@router.post("/people/{person_id}/delete", response_class=HTMLResponse)
async def delete_person(
    person_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Cascade-delete junction records then Person."""
    person = (await db.execute(
        select(Person).where(Person.person_id == person_id)
    )).scalars().first()

    if not person:
        return HTMLResponse("Not found", status_code=404)

    # Delete junction records first
    filer_links = (await db.execute(
        select(FilerPerson).where(FilerPerson.person_id == person_id)
    )).scalars().all()
    for link in filer_links:
        await db.delete(link)

    txn_links = (await db.execute(
        select(TransactionPerson).where(TransactionPerson.person_id == person_id)
    )).scalars().all()
    for link in txn_links:
        await db.delete(link)

    await db.delete(person)
    await db.commit()

    from fastapi.responses import RedirectResponse
    return RedirectResponse("/people", status_code=303)


# ─── Transaction Tagging (5c) ──────────────────────────────


@router.post("/people/link-transaction", response_class=HTMLResponse)
async def link_transaction(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create TransactionPerson link (source='manual', confidence=1.0)."""
    form = await request.form()
    transaction_id = str(form.get("transaction_id", "")).strip()
    person_id = str(form.get("person_id", "")).strip()

    if not transaction_id or not person_id:
        return HTMLResponse("Missing parameters", status_code=400)

    # Check for existing link
    existing = (await db.execute(
        select(TransactionPerson).where(
            TransactionPerson.transaction_id == transaction_id,
            TransactionPerson.person_id == person_id,
        )
    )).scalars().first()

    if not existing:
        link = TransactionPerson(
            transaction_id=transaction_id,
            person_id=person_id,
            match_confidence=1.0,
            needs_review=False,
            source="manual",
        )
        db.add(link)
        await db.commit()

    return await _render_person_tags(transaction_id, db, request)


@router.delete("/people/unlink-transaction", response_class=HTMLResponse)
async def unlink_transaction(
    request: Request,
    transaction_id: str = Query(""),
    person_id: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Delete TransactionPerson link."""
    if not transaction_id or not person_id:
        return HTMLResponse("Missing parameters", status_code=400)

    link = (await db.execute(
        select(TransactionPerson).where(
            TransactionPerson.transaction_id == transaction_id,
            TransactionPerson.person_id == person_id,
        )
    )).scalars().first()

    if link:
        await db.delete(link)
        await db.commit()

    return await _render_person_tags(transaction_id, db, request)


async def _render_person_tags(transaction_id: str, db: AsyncSession, request: Request) -> HTMLResponse:
    """Render person tag chips for a transaction."""
    from app.main import templates

    links = (await db.execute(
        select(TransactionPerson)
        .options(joinedload(TransactionPerson.person))
        .where(TransactionPerson.transaction_id == transaction_id)
    )).unique().scalars().all()

    return templates.TemplateResponse("components/person_tags.html", {
        "request": request,
        "transaction_id": transaction_id,
        "person_links": links,
    })


# ─── Person Merge (5d) ─────────────────────────────────


@router.post("/people/merge", response_class=HTMLResponse)
async def merge_people(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Merge 2+ people into one canonical person."""
    form = await request.form()
    winner_id = str(form.get("winner_id", "")).strip()
    all_ids = str(form.get("all_ids", "")).strip()

    person_ids = [i.strip() for i in all_ids.split(",") if i.strip()]
    loser_ids = [pid for pid in person_ids if pid != winner_id]

    if not winner_id or not loser_ids:
        return HTMLResponse("<p class='text-muted'>Invalid merge parameters.</p>", status_code=400)

    winner = (await db.execute(
        select(Person).where(Person.person_id == winner_id)
    )).scalars().first()

    if not winner:
        return HTMLResponse("<p class='text-muted'>Winner not found.</p>", status_code=404)

    for loser_id in loser_ids:
        loser = (await db.execute(
            select(Person).where(Person.person_id == loser_id)
        )).scalars().first()

        if not loser:
            continue

        # Add loser's name(s) to winner's aliases
        aliases = []
        if winner.aliases:
            try:
                aliases = json.loads(winner.aliases) if isinstance(winner.aliases, str) else winner.aliases
            except (json.JSONDecodeError, TypeError):
                aliases = []
        if loser.canonical_name not in aliases and loser.canonical_name != winner.canonical_name:
            aliases.append(loser.canonical_name)
        if loser.aliases:
            try:
                loser_aliases = json.loads(loser.aliases) if isinstance(loser.aliases, str) else loser.aliases
                for a in loser_aliases:
                    if a not in aliases and a != winner.canonical_name:
                        aliases.append(a)
            except (json.JSONDecodeError, TypeError):
                pass
        winner.aliases = json.dumps(aliases) if aliases else None

        # Re-assign FilerPerson links
        filer_links = (await db.execute(
            select(FilerPerson).where(FilerPerson.person_id == loser_id)
        )).scalars().all()
        for fl in filer_links:
            existing = (await db.execute(
                select(FilerPerson).where(
                    FilerPerson.filer_id == fl.filer_id,
                    FilerPerson.person_id == winner_id,
                    FilerPerson.role == fl.role,
                )
            )).scalars().first()
            if existing:
                await db.delete(fl)
            else:
                fl.person_id = winner_id

        # Re-assign TransactionPerson links
        txn_links = (await db.execute(
            select(TransactionPerson).where(TransactionPerson.person_id == loser_id)
        )).scalars().all()
        for tl in txn_links:
            existing = (await db.execute(
                select(TransactionPerson).where(
                    TransactionPerson.transaction_id == tl.transaction_id,
                    TransactionPerson.person_id == winner_id,
                )
            )).scalars().first()
            if existing:
                await db.delete(tl)
            else:
                tl.person_id = winner_id

        await db.delete(loser)

    await db.commit()

    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/people/{winner_id}", status_code=303)
