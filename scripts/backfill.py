"""Backfill CLI — download historical campaign finance data 2020-2026.

Usage:
  python scripts/backfill.py                    # Default: 2020-2026
  python scripts/backfill.py --years 2024-2026  # Specific range
  python scripts/backfill.py --skip-pdfs        # Skip PDF downloads
  python scripts/backfill.py --skip-enrich      # Skip filing metadata API calls
  python scripts/backfill.py --filers-only      # Just sync filer list
  python scripts/backfill.py --resume           # Resume interrupted run

The portal's bulk Excel export is the only way to get historical
transaction data — the NetFile API has no "list filings" endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path so we can import app.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, func

from app.config import SCRAPE_RATE_LIMIT, EXPORT_STORAGE_PATH
from app.db import AsyncSessionLocal, init_db
from app.models import Filer, Filing, Transaction, ScrapeLog
from app.services.netfile_api import NetFileClient
from app.services.portal_export import PortalSession
from app.services.excel_parser import parse_excel_export
from app.services.pdf_downloader import download_filing_pdf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill")

# ─── Globals for graceful shutdown ───────────────────────────
_interrupted = False
_resume_state: dict = {}  # {year: last_report_num_processed}
RESUME_FILE = EXPORT_STORAGE_PATH / ".backfill_resume.json"


def _handle_sigint(sig, frame):
    global _interrupted
    if _interrupted:
        logger.warning("Force quit.")
        sys.exit(1)
    _interrupted = True
    logger.warning("Ctrl+C detected — finishing current item then saving resume state...")


signal.signal(signal.SIGINT, _handle_sigint)


def _save_resume_state():
    """Persist resume cursor to disk."""
    if _resume_state:
        RESUME_FILE.write_text(json.dumps(_resume_state, indent=2))
        logger.info("Resume state saved to %s", RESUME_FILE.name)


def _load_resume_state() -> dict:
    """Load resume cursor from disk."""
    if RESUME_FILE.exists():
        data = json.loads(RESUME_FILE.read_text())
        logger.info("Loaded resume state: %s", data)
        return data
    return {}


def _clear_resume_state():
    """Remove resume file after successful completion."""
    if RESUME_FILE.exists():
        RESUME_FILE.unlink()


def _progress_bar(current: int, total: int, width: int = 40) -> str:
    """Simple ASCII progress bar."""
    if total == 0:
        return "[" + " " * width + "] 0%"
    pct = current / total
    filled = int(width * pct)
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {pct:.0%}"


# ─── Step 1: Sync Filers ────────────────────────────────────

async def sync_filers(client: NetFileClient) -> int:
    """Download all filers from the API and upsert into DB.

    Returns count of filers synced.
    """
    logger.info("Syncing filers from API...")
    all_filers = await client.list_all_filers()
    logger.info("Fetched %d filers from API", len(all_filers))

    count = 0
    async with AsyncSessionLocal() as db:
        for filer_data in all_filers:
            filer_id_api = str(filer_data.get("id", ""))
            local_id = str(filer_data.get("localAgencyId", "") or "")
            sos_id = str(filer_data.get("fppcId", "") or "")
            name = filer_data.get("name", "Unknown")
            filer_type = filer_data.get("filerType", "")
            status = filer_data.get("registrationStatus", "")
            office = filer_data.get("officeSought", "")
            jurisdiction = filer_data.get("jurisdiction", "")

            # Try to match by netfile_filer_id first
            result = await db.execute(
                select(Filer).where(Filer.netfile_filer_id == filer_id_api)
            )
            filer = result.scalar_one_or_none()

            if not filer and local_id:
                # Try match by local_filer_id
                result = await db.execute(
                    select(Filer).where(Filer.local_filer_id == local_id)
                )
                filer = result.scalar_one_or_none()

            if filer:
                filer.name = name
                filer.netfile_filer_id = filer_id_api
                if local_id:
                    filer.local_filer_id = local_id
                if sos_id:
                    filer.sos_filer_id = sos_id
                if filer_type:
                    filer.filer_type = filer_type
                if status:
                    filer.status = status
                if office:
                    filer.office = office
                if jurisdiction:
                    filer.jurisdiction = jurisdiction
                filer.updated_at = datetime.now(timezone.utc)
            else:
                filer = Filer(
                    netfile_filer_id=filer_id_api,
                    local_filer_id=local_id or None,
                    sos_filer_id=sos_id or None,
                    name=name,
                    filer_type=filer_type or None,
                    status=status or None,
                    office=office or None,
                    jurisdiction=jurisdiction or None,
                )
                db.add(filer)

            count += 1

        await db.commit()

    logger.info("Synced %d filers", count)
    return count


# ─── Step 2: Process Excel Export ────────────────────────────

async def _find_or_create_filer(db, sos_filer_id: str, filer_name: str) -> str:
    """Match or create a Filer from Excel row data. Returns filer_id."""
    # Try by sos_filer_id first (FPPC ID)
    if sos_filer_id and sos_filer_id != "Pending":
        result = await db.execute(
            select(Filer).where(Filer.sos_filer_id == sos_filer_id)
        )
        filer = result.scalars().first()
        if filer:
            return filer.filer_id

        # Try local_filer_id (some exports use FPPC ID in Filer_ID column)
        result = await db.execute(
            select(Filer).where(Filer.local_filer_id == sos_filer_id)
        )
        filer = result.scalars().first()
        if filer:
            if not filer.sos_filer_id:
                filer.sos_filer_id = sos_filer_id
            return filer.filer_id

    # Try by name
    if filer_name:
        result = await db.execute(
            select(Filer).where(Filer.name == filer_name)
        )
        filer = result.scalars().first()
        if filer:
            if sos_filer_id and not filer.sos_filer_id:
                filer.sos_filer_id = sos_filer_id
            return filer.filer_id

    # Create new
    filer = Filer(
        sos_filer_id=sos_filer_id or None,
        name=filer_name or "Unknown Filer",
    )
    db.add(filer)
    await db.flush()
    return filer.filer_id


async def _find_or_create_filing(
    db, filing_key: str, filer_id: str, row: dict
) -> str:
    """Match or create a Filing from Excel row data. Returns filing_id."""
    # Use filing_key (FilerID_RptDate_AmendSeq) as netfile_filing_id for Excel-sourced filings
    result = await db.execute(
        select(Filing).where(Filing.netfile_filing_id == filing_key)
    )
    filing = result.scalar_one_or_none()

    if filing:
        return filing.filing_id

    filing_date_val = row.get("filing_date")
    if isinstance(filing_date_val, datetime):
        filing_dt = filing_date_val
    elif filing_date_val:
        filing_dt = datetime.combine(filing_date_val, datetime.min.time())
    else:
        filing_dt = datetime.now(timezone.utc)

    amendment_seq_str = row.get("amendment_seq", "0")
    try:
        amendment_seq = int(amendment_seq_str)
    except (ValueError, TypeError):
        amendment_seq = 0

    filing = Filing(
        netfile_filing_id=filing_key,
        filer_id=filer_id,
        form_type="F460",  # Excel export doesn't include form type; F460 is the standard period filing
        filing_date=filing_dt,
        period_start=row.get("period_start"),
        period_end=row.get("period_end"),
        amendment_seq=amendment_seq,
        data_source="excel_export",
    )
    db.add(filing)
    await db.flush()
    return filing.filing_id


async def process_year_export(
    path: Path,
    year: int,
    resume_from: str | None = None,
) -> dict:
    """Parse an Excel export and upsert all records.

    Returns stats dict: {filers, filings, transactions, skipped, errors}.
    """
    global _interrupted

    rows = parse_excel_export(path)
    if not rows:
        logger.warning("No transactions parsed from %s", path.name)
        return {"filers": 0, "filings": 0, "transactions": 0, "skipped": 0, "errors": 0}

    # Group rows by filing_key (FilerID_RptDate_AmendSeq — one filing per group)
    by_filing: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        fk = row.get("filing_key", "")
        if fk:
            by_filing[fk].append(row)
        else:
            by_filing["_no_key"].append(row)

    total_txns = len(rows)
    filing_keys = sorted(by_filing.keys())
    stats = {"filers": 0, "filings": 0, "transactions": 0, "skipped": 0, "errors": 0}

    # If resuming, skip already-processed filing keys
    if resume_from:
        try:
            idx = filing_keys.index(resume_from)
            skipping = filing_keys[:idx + 1]
            filing_keys = filing_keys[idx + 1:]
            skip_count = sum(len(by_filing[fk]) for fk in skipping)
            logger.info("Resuming from filing %s — skipping %d transactions", resume_from, skip_count)
            stats["skipped"] = skip_count
        except ValueError:
            logger.warning("Resume filing_key %s not found — starting from beginning", resume_from)

    seen_filers: set[str] = set()
    processed = stats["skipped"]

    for filing_key in filing_keys:
        if _interrupted:
            _resume_state[str(year)] = filing_key
            _save_resume_state()
            logger.warning("Interrupted at year %d, filing %s", year, filing_key)
            break

        filing_rows = by_filing[filing_key]
        first_row = filing_rows[0]

        async with AsyncSessionLocal() as db:
            try:
                # Upsert filer
                sos_id = first_row.get("sos_filer_id", "")
                filer_name = first_row.get("filer_name", "")
                filer_key = sos_id or filer_name

                filer_id = await _find_or_create_filer(db, sos_id, filer_name)
                if filer_key not in seen_filers:
                    seen_filers.add(filer_key)
                    stats["filers"] += 1

                # Upsert filing
                filing_id = await _find_or_create_filing(
                    db, filing_key, filer_id, first_row
                )
                stats["filings"] += 1

                # Create transactions
                for txn_row in filing_rows:
                    tran_id = txn_row.get("tran_id", "")

                    # Skip duplicates: check by filing_id + netfile_transaction_id
                    if tran_id:
                        result = await db.execute(
                            select(Transaction).where(
                                Transaction.filing_id == filing_id,
                                Transaction.netfile_transaction_id == tran_id,
                            )
                        )
                        if result.scalar_one_or_none():
                            stats["skipped"] += 1
                            processed += 1
                            continue

                    txn = Transaction(
                        filing_id=filing_id,
                        schedule=txn_row.get("schedule") or None,
                        transaction_type_code=txn_row.get("transaction_type_code") or None,
                        entity_name=txn_row.get("entity_name") or None,
                        entity_type=txn_row.get("entity_type") or None,
                        first_name=txn_row.get("first_name") or None,
                        last_name=txn_row.get("last_name") or None,
                        city=txn_row.get("city") or None,
                        state=txn_row.get("state") or None,
                        zip_code=txn_row.get("zip_code") or None,
                        employer=txn_row.get("employer") or None,
                        occupation=txn_row.get("occupation") or None,
                        amount=txn_row["amount"],
                        cumulative_amount=txn_row.get("cumulative_amount"),
                        transaction_date=txn_row.get("transaction_date"),
                        description=txn_row.get("description") or None,
                        memo_code=txn_row.get("memo_code", False),
                        netfile_transaction_id=tran_id or None,
                        data_source="excel_export",
                    )
                    db.add(txn)
                    stats["transactions"] += 1
                    processed += 1

                await db.commit()

            except Exception as e:
                logger.error("Error processing filing %s: %s", filing_key, e)
                stats["errors"] += 1
                await db.rollback()

        # Progress output
        print(f"\r  {year}: {processed}/{total_txns} transactions {_progress_bar(processed, total_txns)}", end="", flush=True)

    print()  # newline after progress bar
    return stats


# ─── Step 3: Enrich Filings ─────────────────────────────────

async def enrich_filings(client: NetFileClient) -> int:
    """Fetch API metadata for filings that came from Excel export.

    Returns count of filings enriched.
    """
    logger.info("Enriching Excel-sourced filings with API metadata...")
    count = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Filing).where(
                Filing.data_source == "excel_export",
                Filing.raw_data.is_(None),
            )
        )
        filings = result.scalars().all()

    logger.info("Found %d filings to enrich", len(filings))

    for i, filing in enumerate(filings, 1):
        if _interrupted:
            break

        try:
            api_data = await client.get_filing_info(filing.netfile_filing_id)

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Filing).where(Filing.filing_id == filing.filing_id)
                )
                f = result.scalar_one()
                f.raw_data = json.dumps(api_data)
                f.is_efiled = api_data.get("isEfiled", False)
                f.amendment_seq = api_data.get("amendmentSequenceNumber", 0)
                f.amends_filing = api_data.get("amends")
                f.amended_by = api_data.get("amendedBy")
                f.efiling_vendor = api_data.get("vendor")
                f.updated_at = datetime.now(timezone.utc)
                await db.commit()

            count += 1
            print(f"\r  Enriched: {i}/{len(filings)} {_progress_bar(i, len(filings))}", end="", flush=True)

        except Exception as e:
            logger.debug("Could not enrich filing %s: %s", filing.netfile_filing_id, e)

        await asyncio.sleep(SCRAPE_RATE_LIMIT)

    print()
    logger.info("Enriched %d filings", count)
    return count


# ─── Step 4: Download PDFs ──────────────────────────────────

async def download_pdfs(client: NetFileClient) -> int:
    """Download PDFs for filings that don't have them yet.

    Returns count of PDFs downloaded.
    """
    logger.info("Downloading missing PDFs...")
    count = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Filing).where(Filing.pdf_downloaded == False)
        )
        filings = result.scalars().all()

    logger.info("Found %d filings without PDFs", len(filings))

    for i, filing in enumerate(filings, 1):
        if _interrupted:
            break

        try:
            success, pdf_path, pdf_size = await download_filing_pdf(
                client, filing.netfile_filing_id
            )
            if success:
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(Filing).where(Filing.filing_id == filing.filing_id)
                    )
                    f = result.scalar_one()
                    f.pdf_path = pdf_path
                    f.pdf_size = pdf_size
                    f.pdf_downloaded = True
                    await db.commit()
                count += 1
        except Exception as e:
            logger.debug("PDF download failed for %s: %s", filing.netfile_filing_id, e)

        print(f"\r  PDFs: {i}/{len(filings)} ({count} downloaded) {_progress_bar(i, len(filings))}", end="", flush=True)
        await asyncio.sleep(SCRAPE_RATE_LIMIT)

    print()
    logger.info("Downloaded %d PDFs", count)
    return count


# ─── Main Pipeline ───────────────────────────────────────────

async def main(args: argparse.Namespace):
    global _interrupted, _resume_state

    # Initialize database
    # Import models so tables are registered
    import app.models  # noqa: F401
    await init_db()

    client = NetFileClient()
    portal = PortalSession()

    # Parse year range
    if args.years:
        if "-" in args.years:
            start, end = args.years.split("-", 1)
            years = list(range(int(start), int(end) + 1))
        else:
            years = [int(args.years)]
    else:
        years = list(range(2020, 2027))

    # Load resume state if requested
    if args.resume:
        _resume_state = _load_resume_state()

    # Create scrape log
    async with AsyncSessionLocal() as db:
        log_entry = ScrapeLog(
            scrape_type="backfill",
            status="running",
            parameters=json.dumps({
                "years": years,
                "skip_pdfs": args.skip_pdfs,
                "skip_enrich": args.skip_enrich,
                "filers_only": args.filers_only,
                "resume": args.resume,
            }),
        )
        db.add(log_entry)
        await db.commit()
        log_id = log_entry.log_id

    grand_total = {"filers": 0, "filings": 0, "transactions": 0, "skipped": 0, "errors": 0}

    try:
        # ── Step 1: Sync filers from API ──
        print("\n=== Step 1: Sync Filers ===")
        filer_count = await sync_filers(client)
        grand_total["filers"] = filer_count

        if args.filers_only:
            print(f"\nFilers synced: {filer_count}")
            return

        # ── Step 2: Download + process Excel exports per year ──
        print(f"\n=== Step 2: Process Years {years[0]}-{years[-1]} ===")

        for year in years:
            if _interrupted:
                break

            print(f"\n--- Year {year} ---")

            # Check for existing export or download
            export_path = EXPORT_STORAGE_PATH / f"CSHA_{year}_amended.xlsx"
            if export_path.exists():
                size_kb = export_path.stat().st_size / 1024
                logger.info("Using existing export: %s (%.1f KB)", export_path.name, size_kb)
            else:
                try:
                    export_path = await portal.download_year_export(year)
                except Exception as e:
                    logger.error("Failed to download export for %d: %s", year, e)
                    continue

            # Process the export
            resume_from = _resume_state.get(str(year)) if args.resume else None
            year_stats = await process_year_export(export_path, year, resume_from)

            # Accumulate stats
            for key in grand_total:
                if key != "filers":
                    grand_total[key] += year_stats.get(key, 0)

            print(f"  Year {year}: {year_stats['filings']} filings, "
                  f"{year_stats['transactions']} transactions, "
                  f"{year_stats['skipped']} skipped, "
                  f"{year_stats['errors']} errors")

        # ── Step 3: Enrich filings with API metadata ──
        if not args.skip_enrich and not _interrupted:
            print("\n=== Step 3: Enrich Filings ===")
            await enrich_filings(client)

        # ── Step 4: Download PDFs ──
        if not args.skip_pdfs and not _interrupted:
            print("\n=== Step 4: Download PDFs ===")
            await download_pdfs(client)

        # ── Summary ──
        print("\n" + "=" * 50)
        print("BACKFILL COMPLETE")
        print("=" * 50)

        async with AsyncSessionLocal() as db:
            filer_count = (await db.execute(select(func.count()).select_from(Filer))).scalar()
            filing_count = (await db.execute(select(func.count()).select_from(Filing))).scalar()
            txn_count = (await db.execute(select(func.count()).select_from(Transaction))).scalar()

        print(f"  Filers in DB:       {filer_count}")
        print(f"  Filings in DB:      {filing_count}")
        print(f"  Transactions in DB: {txn_count}")
        print(f"\n  This run:")
        print(f"    Filings created:  {grand_total['filings']}")
        print(f"    Transactions:     {grand_total['transactions']}")
        print(f"    Skipped (dupes):  {grand_total['skipped']}")
        print(f"    Errors:           {grand_total['errors']}")

        # Update scrape log
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ScrapeLog).where(ScrapeLog.log_id == log_id)
            )
            log = result.scalar_one()
            log.status = "completed" if not _interrupted else "interrupted"
            log.completed_at = datetime.now(timezone.utc)
            log.items_processed = grand_total["transactions"]
            await db.commit()

        if not _interrupted:
            _clear_resume_state()

    except Exception as e:
        logger.exception("Backfill pipeline error: %s", e)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ScrapeLog).where(ScrapeLog.log_id == log_id)
            )
            log = result.scalar_one()
            log.status = "error"
            log.completed_at = datetime.now(timezone.utc)
            log.error_message = str(e)
            await db.commit()
        raise

    finally:
        await client.close()
        await portal.close()


def cli():
    parser = argparse.ArgumentParser(
        description="Backfill historical campaign finance data from NetFile portal exports."
    )
    parser.add_argument(
        "--years",
        type=str,
        default=None,
        help="Year or range (e.g. '2024' or '2020-2026'). Default: 2020-2026",
    )
    parser.add_argument(
        "--skip-pdfs",
        action="store_true",
        help="Skip PDF downloads",
    )
    parser.add_argument(
        "--skip-enrich",
        action="store_true",
        help="Skip filing metadata enrichment from API",
    )
    parser.add_argument(
        "--filers-only",
        action="store_true",
        help="Only sync the filer list from the API, then exit",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an interrupted backfill run",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = cli()
    asyncio.run(main(args))
