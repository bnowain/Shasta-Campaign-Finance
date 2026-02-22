"""Background tasks for the Settings page.

Two async tasks with locking:
- run_check_elections: sync filers, scrape portal, relink candidates
- run_check_filings: sync filers, check watched filers, discover+ingest filings
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, func

from app.db import AsyncSessionLocal
from app.models import Filer, Filing, WatchedFiler
from app.services.candidate_matcher import relink_candidates
from app.services.netfile_api import NetFileClient
from app.services.settings_state import settings_state

logger = logging.getLogger(__name__)

_task_lock = asyncio.Lock()


def is_task_running() -> bool:
    return _task_lock.locked()


async def _sync_filers_from_api(client: NetFileClient) -> int:
    """Sync filer list from NetFile API. Update-only: fill empty fields, never overwrite.

    Returns count of filers synced/updated.
    """
    api_filers = await client.list_all_filers()
    count = 0

    async with AsyncSessionLocal() as db:
        for af in api_filers:
            netfile_id = af.get("id") or af.get("filingId") or ""
            fppc_id = af.get("fppcId", "")
            local_id = af.get("localAgencyId", "")
            name = af.get("name", "")

            if not name:
                continue

            # Try to find existing filer by netfile ID or local ID
            existing = None
            if netfile_id:
                existing = (await db.execute(
                    select(Filer).where(Filer.netfile_filer_id == str(netfile_id))
                )).scalars().first()

            if not existing and local_id:
                existing = (await db.execute(
                    select(Filer).where(Filer.local_filer_id == local_id)
                )).scalars().first()

            if existing:
                # Update-only: fill empty fields
                if not existing.netfile_filer_id and netfile_id:
                    existing.netfile_filer_id = str(netfile_id)
                if not existing.local_filer_id and local_id:
                    existing.local_filer_id = local_id
                if not existing.sos_filer_id and fppc_id:
                    existing.sos_filer_id = fppc_id
                existing.updated_at = datetime.now(timezone.utc)
            else:
                filer = Filer(
                    netfile_filer_id=str(netfile_id) if netfile_id else None,
                    local_filer_id=local_id or None,
                    sos_filer_id=fppc_id or None,
                    name=name,
                )
                db.add(filer)

            count += 1

        await db.commit()

    return count


async def run_check_elections():
    """Check Elections background task:
    1. Sync filer list from NetFile API
    2. Run portal scrape to discover elections + candidates
    3. Relink candidates to filers with filings
    """
    async with _task_lock:
        settings_state.start("elections")

        try:
            client = NetFileClient()

            # Step 1: Sync filers
            settings_state.set_progress(1, 3, "Syncing filers",
                                        "Fetching filer list from NetFile API...")
            filers_synced = await _sync_filers_from_api(client)
            await client.close()
            logger.info("Synced %d filers from API", filers_synced)

            # Step 2: Portal scrape
            settings_state.set_progress(2, 3, "Scraping elections",
                                        "Scraping NetFile portal for elections...")
            from scripts.election_ingest import phase_a
            await phase_a(min_year=2016, max_year=2026)

            # Count elections
            async with AsyncSessionLocal() as db:
                elections_count = (await db.execute(
                    select(func.count()).select_from(
                        select(Filer.filer_id).subquery()  # placeholder
                    )
                )).scalar() or 0

            # Step 3: Relink candidates
            settings_state.set_progress(3, 3, "Relinking candidates",
                                        "Matching candidates to campaign committees...")
            async with AsyncSessionLocal() as db:
                summary = await relink_candidates(db, dry_run=False)

            settings_state.set_complete(
                filers_synced=filers_synced,
                elections_found=summary.get("total_unlinked", 0),
                candidates_linked=summary.get("matched", 0),
            )
            logger.info("Check Elections complete: %d filers, %d candidates linked",
                        filers_synced, summary.get("matched", 0))

        except Exception as e:
            logger.exception("Check Elections error: %s", e)
            settings_state.set_error(str(e))


async def run_check_filings():
    """Check Filings background task:
    1. Sync filer list from NetFile API
    2. Search API for WatchedFiler names
    3. Check RSS for new filings
    4. Auto-ingest discovered filings
    """
    async with _task_lock:
        settings_state.start("filings")

        try:
            client = NetFileClient()

            # Step 1: Sync filers
            settings_state.set_progress(1, 4, "Syncing filers",
                                        "Fetching filer list from NetFile API...")
            filers_synced = await _sync_filers_from_api(client)
            logger.info("Synced %d filers from API", filers_synced)

            # Step 2: Check watched filers
            settings_state.set_progress(2, 4, "Checking watched filers",
                                        "Searching for watched filer names...")
            async with AsyncSessionLocal() as db:
                watched = (await db.execute(select(WatchedFiler))).scalars().all()
                watched_count = 0
                for wf in watched:
                    # Check if a filer with this name already exists
                    existing = (await db.execute(
                        select(Filer).where(
                            func.lower(Filer.name) == wf.name.lower().strip()
                        )
                    )).scalars().first()
                    if not existing:
                        # Create a placeholder filer for this watched name
                        new_filer = Filer(name=wf.name, filer_type="candidate")
                        db.add(new_filer)
                        watched_count += 1
                await db.commit()
            logger.info("Created %d filers from watched list", watched_count)

            # Step 3: Check RSS for new filings
            settings_state.set_progress(3, 4, "Checking RSS",
                                        "Scanning RSS feed for new filings...")
            from app.services.rss_monitor import discover_new_filings
            async with AsyncSessionLocal() as db:
                discovered = await discover_new_filings(client, db)
            logger.info("RSS: %d new filings discovered", len(discovered))

            # Step 4: Ingest discovered filings
            filings_ingested = 0
            if discovered:
                settings_state.set_progress(4, 4, "Ingesting filings",
                                            f"Ingesting {len(discovered)} new filings...")
                from app.services.pull_pipeline import run_ingest
                # Run ingest directly (not as background task since we're already in one)
                await run_ingest(discovered)
                filings_ingested = len(discovered)
            else:
                settings_state.set_progress(4, 4, "No new filings",
                                            "No new filings found in RSS feed.")

            await client.close()

            settings_state.set_complete(
                filers_synced=filers_synced,
                filings_discovered=len(discovered),
                filings_ingested=filings_ingested,
            )
            logger.info("Check Filings complete: %d filers, %d filings",
                        filers_synced, filings_ingested)

        except Exception as e:
            logger.exception("Check Filings error: %s", e)
            settings_state.set_error(str(e))
