"""Async ingest pipeline for the Pull operation.

Orchestrates the 3-phase pull: discover → confirm → ingest.
Each filing goes through: metadata fetch → PDF download → efile extraction.
Uses an asyncio.Lock to prevent concurrent pulls.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from datetime import datetime, date, timezone

from sqlalchemy import select

from app.config import SCRAPE_RATE_LIMIT
from app.db import AsyncSessionLocal
from app.models import Filer, Filing, Transaction, ScrapeLog
from app.services.cal_parser import parse_cal_transactions
from app.services.netfile_api import NetFileClient
from app.services.pdf_downloader import download_filing_pdf
from app.services.pull_state import pull_state
from app.services.rss_monitor import DiscoveredFiling, update_feed_state

logger = logging.getLogger(__name__)

_pull_lock = asyncio.Lock()


def is_pull_running() -> bool:
    """Check if a pull operation is currently in progress."""
    return _pull_lock.locked()


def _parse_iso_datetime(val: str | None) -> datetime | None:
    """Parse an ISO datetime string from the API, tolerating the .0000000 format."""
    if not val:
        return None
    # Strip the extra precision and timezone offset for parsing
    try:
        # Handle "2026-02-19T11:41:10.3030000-08:00" format
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        try:
            # Fallback: truncate fractional seconds
            base = val.split(".")[0]
            return datetime.fromisoformat(base)
        except (ValueError, TypeError):
            return None


def _parse_iso_date(val: str | None) -> date | None:
    """Parse an ISO datetime string to just a date."""
    dt = _parse_iso_datetime(val)
    return dt.date() if dt else None


def _extract_cal_text(zip_bytes: bytes) -> str | None:
    """Extract Efile.txt from ZIP bytes."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".txt"):
                    return zf.read(name).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, Exception) as e:
        logger.error("Failed to extract CAL from ZIP: %s", e)
    return None


async def _upsert_filer(db, api_data: dict) -> str:
    """Find or create a Filer from flat API filing info fields.

    Returns the filer_id (our UUID PK).
    """
    local_filer_id = api_data.get("localFilerId", "")
    sos_filer_id = api_data.get("sosFilerId", "")
    filer_name = api_data.get("filerName", "Unknown Filer")

    # Try to match by local_filer_id first
    result = await db.execute(
        select(Filer).where(Filer.local_filer_id == local_filer_id)
    )
    filer = result.scalar_one_or_none()

    if filer:
        # Update fields that may have changed
        filer.name = filer_name
        if sos_filer_id:
            filer.sos_filer_id = sos_filer_id
        filer.updated_at = datetime.now(timezone.utc)
    else:
        filer = Filer(
            local_filer_id=local_filer_id,
            sos_filer_id=sos_filer_id if sos_filer_id else None,
            name=filer_name,
        )
        db.add(filer)
        await db.flush()  # Generate PK

    return filer.filer_id


async def _upsert_filing(
    db, api_data: dict, discovered: DiscoveredFiling, filer_id: str
) -> str:
    """Create or update a Filing record from API data.

    Returns the filing_id (our UUID PK).
    """
    netfile_filing_id = api_data.get("filingId", discovered.netfile_filing_id)

    # Check if filing already exists
    result = await db.execute(
        select(Filing).where(Filing.netfile_filing_id == netfile_filing_id)
    )
    filing = result.scalar_one_or_none()

    # Form description comes from RSS since API formName is always null
    form_description = discovered.form_description

    if filing:
        filing.filer_id = filer_id
        filing.form_name = form_description
        filing.updated_at = datetime.now(timezone.utc)
    else:
        filing = Filing(
            netfile_filing_id=netfile_filing_id,
            filer_id=filer_id,
            form_type=_extract_form_type(form_description),
            form_name=form_description,
            filing_date=_parse_iso_datetime(api_data.get("filingDate")) or datetime.now(timezone.utc),
            period_start=_parse_iso_date(api_data.get("dateStart")),
            period_end=_parse_iso_date(api_data.get("dateEnd")),
            amendment_seq=api_data.get("amendmentSequenceNumber", 0),
            amends_filing=api_data.get("amends"),
            amended_by=api_data.get("amendedBy"),
            is_efiled=api_data.get("isEfiled", False),
            efiling_vendor=api_data.get("vendor"),
            raw_data=json.dumps(api_data),
        )
        db.add(filing)
        await db.flush()

    return filing.filing_id


def _extract_form_type(description: str) -> str:
    """Extract form type code from RSS description like 'FPPC Form 460 (1/1/2026 - 2/18/2026)'."""
    if not description:
        return "unknown"
    # Try to find "Form NNN" pattern
    desc_upper = description.upper()
    for form in ("460", "410", "496", "497", "450", "461"):
        if form in desc_upper:
            return f"F{form}"
    return "unknown"


async def _create_transactions_from_cal(db, filing_id: str, cal_text: str) -> int:
    """Parse CAL text and create Transaction records. Returns count created."""
    parsed = parse_cal_transactions(cal_text)
    count = 0
    for txn_data in parsed:
        txn = Transaction(
            filing_id=filing_id,
            schedule=txn_data.get("schedule"),
            transaction_type=txn_data.get("transaction_type"),
            transaction_type_code=txn_data.get("transaction_type_code"),
            entity_name=txn_data.get("entity_name"),
            entity_type=txn_data.get("entity_type"),
            first_name=txn_data.get("first_name"),
            last_name=txn_data.get("last_name"),
            city=txn_data.get("city"),
            state=txn_data.get("state"),
            zip_code=txn_data.get("zip_code"),
            employer=txn_data.get("employer"),
            occupation=txn_data.get("occupation"),
            amount=txn_data["amount"],
            cumulative_amount=txn_data.get("cumulative_amount"),
            transaction_date=txn_data.get("transaction_date"),
            description=txn_data.get("description"),
            memo_code=txn_data.get("memo_code", False),
            netfile_transaction_id=txn_data.get("netfile_transaction_id"),
            data_source="efile",
        )
        db.add(txn)
        count += 1
    return count


async def run_ingest(discovered: list[DiscoveredFiling]):
    """Main ingest pipeline — runs as a background task.

    For each discovered filing:
    1. Fetch filing metadata from API
    2. Upsert filer + filing records
    3. Download PDF
    4. If e-filed: download ZIP, extract CAL, create transactions
    """
    if not discovered:
        pull_state.set_complete(0, 0, 0)
        return

    async with _pull_lock:
        client = NetFileClient()
        total = len(discovered)
        filings_count = 0
        pdfs_count = 0
        txns_count = 0

        pull_state.start_timer()

        # Create scrape log entry
        async with AsyncSessionLocal() as db:
            log_entry = ScrapeLog(
                scrape_type="rss_pull",
                status="running",
                items_total=total,
            )
            db.add(log_entry)
            await db.commit()
            log_id = log_entry.log_id

        try:
            for i, filing in enumerate(discovered, 1):
                # Phase 1: Fetch metadata
                pull_state.set_ingesting(i, total, filing.filer_name, "metadata")

                try:
                    api_data = await client.get_filing_info(filing.netfile_filing_id)
                except Exception as e:
                    logger.error("Failed to get filing info for %s: %s", filing.netfile_filing_id, e)
                    continue

                await asyncio.sleep(SCRAPE_RATE_LIMIT)

                # Phase 2: Upsert filer + filing
                async with AsyncSessionLocal() as db:
                    filer_id = await _upsert_filer(db, api_data)
                    filing_id = await _upsert_filing(db, api_data, filing, filer_id)
                    await db.commit()
                    filings_count += 1

                # Phase 3: Download PDF
                pull_state.set_ingesting(i, total, filing.filer_name, "pdf")

                try:
                    success, pdf_path, pdf_size = await download_filing_pdf(
                        client, filing.netfile_filing_id
                    )
                    if success:
                        pdfs_count += 1
                        # Update filing with PDF info
                        async with AsyncSessionLocal() as db:
                            result = await db.execute(
                                select(Filing).where(Filing.filing_id == filing_id)
                            )
                            f = result.scalar_one()
                            f.pdf_path = pdf_path
                            f.pdf_size = pdf_size
                            f.pdf_downloaded = True
                            await db.commit()
                except Exception as e:
                    logger.error("PDF download failed for %s: %s", filing.netfile_filing_id, e)

                await asyncio.sleep(SCRAPE_RATE_LIMIT)

                # Phase 4: E-file extraction (only for e-filed filings)
                is_efiled = api_data.get("isEfiled", False)
                efile_size = api_data.get("efileSize", 0)

                if is_efiled and efile_size and efile_size > 0:
                    pull_state.set_ingesting(i, total, filing.filer_name, "transactions")

                    try:
                        zip_bytes = await client.get_efile_data(filing.netfile_filing_id)
                        cal_text = _extract_cal_text(zip_bytes)

                        if cal_text:
                            async with AsyncSessionLocal() as db:
                                created = await _create_transactions_from_cal(db, filing_id, cal_text)
                                await db.commit()
                                txns_count += created
                    except Exception as e:
                        logger.warning(
                            "E-file extraction failed for %s: %s",
                            filing.netfile_filing_id, e,
                        )

                    await asyncio.sleep(SCRAPE_RATE_LIMIT)

            # Update feed state with the first (newest) guid
            async with AsyncSessionLocal() as db:
                await update_feed_state(db, discovered[0].guid)

            # Update scrape log
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(ScrapeLog).where(ScrapeLog.log_id == log_id)
                )
                log = result.scalar_one()
                log.status = "completed"
                log.completed_at = datetime.now(timezone.utc)
                log.items_processed = filings_count
                await db.commit()

            pull_state.set_complete(filings_count, pdfs_count, txns_count)

        except Exception as e:
            logger.exception("Pull pipeline error: %s", e)

            # Update scrape log with error
            try:
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(ScrapeLog).where(ScrapeLog.log_id == log_id)
                    )
                    log = result.scalar_one()
                    log.status = "error"
                    log.completed_at = datetime.now(timezone.utc)
                    log.error_message = str(e)
                    await db.commit()
            except Exception:
                pass

            pull_state.set_error(str(e))

        finally:
            await client.close()
