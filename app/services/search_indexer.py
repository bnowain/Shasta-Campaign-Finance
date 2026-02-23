"""FTS5 search index management — rebuild and query."""

from sqlalchemy import text, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import Filer, Transaction, Election, Person, FilerPerson, TransactionPerson


async def rebuild_search_index():
    """Rebuild FTS5 search_index from filers + transactions."""
    async with AsyncSessionLocal() as session:
        # Clear existing index
        await session.execute(text("DELETE FROM search_index"))

        # Index filers
        filers = (await session.execute(select(Filer))).scalars().all()
        for filer in filers:
            context = " ".join(filter(None, [
                filer.filer_type, filer.status, filer.office,
                filer.local_filer_id, filer.jurisdiction,
            ]))
            await session.execute(
                text(
                    "INSERT INTO search_index(entity_type, entity_id, name, context) "
                    "VALUES(:etype, :eid, :name, :ctx)"
                ),
                {"etype": "filer", "eid": filer.filer_id, "name": filer.name, "ctx": context},
            )

        # Index transactions (unique entity names only)
        txn_names = (await session.execute(
            select(
                Transaction.entity_name,
                func.count(Transaction.transaction_id).label("cnt"),
                func.sum(Transaction.amount).label("total"),
            )
            .where(Transaction.entity_name.isnot(None))
            .where(Transaction.entity_name != "")
            .group_by(Transaction.entity_name)
        )).all()

        for name, cnt, total in txn_names:
            context = f"{cnt} transactions, ${total:,.2f} total"
            await session.execute(
                text(
                    "INSERT INTO search_index(entity_type, entity_id, name, context) "
                    "VALUES(:etype, :eid, :name, :ctx)"
                ),
                {"etype": "transaction_entity", "eid": name, "name": name, "ctx": context},
            )

        # Index elections
        elections = (await session.execute(select(Election))).scalars().all()
        for elec in elections:
            context = " ".join(filter(None, [
                elec.election_type, str(elec.year) if elec.year else None,
                elec.data_source,
            ]))
            await session.execute(
                text(
                    "INSERT INTO search_index(entity_type, entity_id, name, context) "
                    "VALUES(:etype, :eid, :name, :ctx)"
                ),
                {"etype": "election", "eid": elec.election_id, "name": elec.name, "ctx": context},
            )

        # Index people
        people = (await session.execute(select(Person))).scalars().all()
        for person in people:
            # Count links
            filer_count = (await session.execute(
                select(func.count(FilerPerson.id)).where(FilerPerson.person_id == person.person_id)
            )).scalar() or 0
            txn_count = (await session.execute(
                select(func.count(TransactionPerson.id)).where(TransactionPerson.person_id == person.person_id)
            )).scalar() or 0
            context = " ".join(filter(None, [
                person.entity_type or "",
                f"{filer_count} filer links" if filer_count else "",
                f"{txn_count} transaction links" if txn_count else "",
            ]))
            await session.execute(
                text(
                    "INSERT INTO search_index(entity_type, entity_id, name, context) "
                    "VALUES(:etype, :eid, :name, :ctx)"
                ),
                {"etype": "person", "eid": person.person_id, "name": person.canonical_name, "ctx": context},
            )

        await session.commit()
        return {
            "filers": len(filers),
            "transaction_entities": len(txn_names),
            "elections": len(elections),
            "people": len(people),
        }


async def search_fts(query: str, limit: int = 20, db: AsyncSession = None):
    """Search FTS5 index. Falls back to LIKE queries when FTS5 is empty."""
    if not query or not query.strip():
        return []

    should_close = False
    if db is None:
        db = AsyncSessionLocal()
        should_close = True

    try:
        # Check if FTS5 has any data
        fts_count = (await db.execute(
            text("SELECT COUNT(*) FROM search_index")
        )).scalar() or 0

        if fts_count > 0:
            # Use FTS5 search
            # Escape special FTS5 characters and add prefix matching
            safe_query = query.replace('"', '""')
            fts_query = f'"{safe_query}"*'

            results = (await db.execute(
                text(
                    "SELECT entity_type, entity_id, name, context, "
                    "rank FROM search_index WHERE search_index MATCH :q "
                    "ORDER BY rank LIMIT :lim"
                ),
                {"q": fts_query, "lim": limit},
            )).all()

            return [
                {
                    "entity_type": r[0],
                    "entity_id": r[1],
                    "name": r[2],
                    "context": r[3],
                    "rank": r[4],
                }
                for r in results
            ]
        else:
            # Fallback: LIKE queries on filers + transactions
            results = []
            pattern = f"%{query}%"

            filers = (await db.execute(
                select(Filer).where(Filer.name.ilike(pattern)).limit(limit)
            )).scalars().all()
            for f in filers:
                results.append({
                    "entity_type": "filer",
                    "entity_id": f.filer_id,
                    "name": f.name,
                    "context": f.filer_type or "",
                    "rank": 0,
                })

            remaining = limit - len(results)
            if remaining > 0:
                elecs = (await db.execute(
                    select(Election).where(Election.name.ilike(pattern)).limit(remaining)
                )).scalars().all()
                for e in elecs:
                    results.append({
                        "entity_type": "election",
                        "entity_id": e.election_id,
                        "name": e.name,
                        "context": e.election_type or "",
                        "rank": 0,
                    })

            remaining = limit - len(results)
            if remaining > 0:
                people = (await db.execute(
                    select(Person).where(Person.canonical_name.ilike(pattern)).limit(remaining)
                )).scalars().all()
                for p in people:
                    results.append({
                        "entity_type": "person",
                        "entity_id": p.person_id,
                        "name": p.canonical_name,
                        "context": p.entity_type or "",
                        "rank": 0,
                    })

            remaining = limit - len(results)
            if remaining > 0:
                txns = (await db.execute(
                    select(
                        Transaction.entity_name,
                        func.count(Transaction.transaction_id).label("cnt"),
                    )
                    .where(Transaction.entity_name.ilike(pattern))
                    .group_by(Transaction.entity_name)
                    .limit(remaining)
                )).all()
                for name, cnt in txns:
                    results.append({
                        "entity_type": "transaction_entity",
                        "entity_id": name,
                        "name": name,
                        "context": f"{cnt} transactions",
                        "rank": 0,
                    })

            return results
    finally:
        if should_close:
            await db.close()
