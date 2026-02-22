"""E-file backfill CLI — discover real filing IDs, parse all schedules, download PDFs.

Usage:
  python scripts/efile_backfill.py                # Full backfill
  python scripts/efile_backfill.py --dry-run      # Report what would be done
  python scripts/efile_backfill.py --skip-pdfs    # Skip PDF downloads
  python scripts/efile_backfill.py --skip-efiles  # Skip e-file parsing
  python scripts/efile_backfill.py --resume       # Skip already-processed filings

Discovery strategy:
  1. Search each DB filer name on the NetFile public portal
  2. Scrape AllFilingsByFiler.aspx pages to get real numeric filing IDs
  3. Match portal filings to existing DB filings and update IDs
  4. Fetch e-file ZIP data and parse all CAL schedules (A, B1, C, E, F)
  5. Download PDFs for filings with real IDs
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import re
import signal
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timezone, date as date_type
from pathlib import Path

# Add project root to path so we can import app.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, func

from app.config import SCRAPE_RATE_LIMIT, EXPORT_STORAGE_PATH
from app.db import AsyncSessionLocal, init_db
from app.models import Filer, Filing, Transaction, ScrapeLog
from app.services.netfile_api import NetFileClient
from app.services.cal_parser import parse_cal_transactions
from app.services.pdf_downloader import download_filing_pdf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("efile_backfill")

# ─── Globals ─────────────────────────────────────────────────
_interrupted = False
RESUME_FILE = EXPORT_STORAGE_PATH / ".efile_backfill_resume.json"
PORTAL_BASE = "https://public.netfile.com/Pub2"


def _handle_sigint(sig, frame):
    global _interrupted
    if _interrupted:
        logger.warning("Force quit.")
        sys.exit(1)
    _interrupted = True
    logger.warning("Ctrl+C detected — finishing current item then stopping...")


signal.signal(signal.SIGINT, _handle_sigint)


def _progress_bar(current: int, total: int, width: int = 40) -> str:
    if total == 0:
        return "[" + " " * width + "] 0%"
    pct = current / total
    filled = int(width * pct)
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {pct:.0%}"


def _load_resume_set() -> set[str]:
    """Load set of already-processed filing IDs."""
    if RESUME_FILE.exists():
        data = json.loads(RESUME_FILE.read_text())
        return set(data.get("processed_ids", []))
    return set()


def _save_resume_set(processed: set[str]):
    RESUME_FILE.write_text(json.dumps({"processed_ids": sorted(processed)}, indent=2))


def _is_composite_key(netfile_filing_id: str) -> bool:
    """Check if a netfile_filing_id is a composite key from Excel export."""
    if not netfile_filing_id:
        return False
    parts = netfile_filing_id.split("_")
    return len(parts) >= 3 and "-" in parts[1]


# ─── Step 1: Portal Scraping — Discover Filing IDs ───────────

async def _get_portal_viewstate(client) -> tuple[str, str]:
    """Fetch the portal homepage and extract ASP.NET VIEWSTATE."""
    r = await client.get(f"{PORTAL_BASE}/?AID=CSHA")
    text = r.text
    vs = re.search(r'id="__VIEWSTATE"[^>]*value="([^"]*)"', text)
    vsg = re.search(r'id="__VIEWSTATEGENERATOR"[^>]*value="([^"]*)"', text)
    return (vs.group(1) if vs else "", vsg.group(1) if vsg else "")


async def _search_portal_by_name(client, viewstate: str, viewstate_gen: str, query: str) -> list[tuple[str, str]]:
    """Search the portal by name and return (portal_filer_id, filer_name) pairs."""
    form_data = {
        "__VIEWSTATE": viewstate,
        "__VIEWSTATEGENERATOR": viewstate_gen,
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        "ctl00$phBody$quickNameSearch$SearchName": query,
        "ctl00$phBody$quickNameSearch$SubmitButton": "Search",
    }
    r = await client.post(f"{PORTAL_BASE}/Default.aspx?AID=CSHA", data=form_data)
    pairs = re.findall(r'AllFilingsByFiler\.aspx\?id=(\d+)[^"]*"[^>]*>([^<]+)<', r.text)
    return [(pid, name.strip()) for pid, name in pairs]


async def _scrape_filer_filings(client, portal_filer_id: str) -> list[dict]:
    """Scrape AllFilingsByFiler page for filing records.

    Returns list of dicts: {filing_id, filer_name, filing_date, form_type,
                            seq, period_start, period_end, is_paper}
    """
    r = await client.get(
        f"{PORTAL_BASE}/AllFilingsByFiler.aspx?id={portal_filer_id}&aid=CSHA"
    )
    text = r.text
    filings = []

    # Parse table rows: Filing ID | Filed By | Filing Date | Form | Seq# | Rpt# | Covers Period | View
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', text, re.DOTALL)
    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 7:
            continue
        clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

        filing_id = clean[0]
        if not filing_id.isdigit():
            continue

        filer_name = clean[1]
        filing_date_str = clean[2]  # MM/DD/YYYY
        form_type = clean[3]  # e.g. "FPPC 460"
        seq = clean[4]  # "Original" or "Amendment"
        # clean[5] = Rpt#
        period_str = clean[6]  # "(MM/DD/YYYY to MM/DD/YYYY)" or "&nbsp;"
        view_text = clean[7] if len(cells) > 7 else ""
        is_paper = "paper" in view_text.lower()

        # Parse filing date
        filing_date = None
        try:
            m = re.match(r'(\d{2})/(\d{2})/(\d{4})', filing_date_str)
            if m:
                filing_date = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except (ValueError, TypeError):
            pass

        # Parse period dates
        period_start = None
        period_end = None
        pm = re.search(r'\((\d{2}/\d{2}/\d{4})\s+to\s+(\d{2}/\d{2}/\d{4})\)', period_str)
        if pm:
            try:
                ps = pm.group(1).split("/")
                period_start = date_type(int(ps[2]), int(ps[0]), int(ps[1]))
                pe = pm.group(2).split("/")
                period_end = date_type(int(pe[2]), int(pe[0]), int(pe[1]))
            except (ValueError, IndexError):
                pass

        # Normalize form type: "FPPC 460" -> "F460"
        form_normalized = form_type.replace("FPPC ", "F").replace(" ", "")

        amendment_seq = 0
        if "amend" in seq.lower():
            # Try to extract amendment number
            am = re.search(r'(\d+)', seq)
            amendment_seq = int(am.group(1)) if am else 1

        filings.append({
            "filing_id": filing_id,
            "filer_name": filer_name,
            "filing_date": filing_date,
            "form_type": form_normalized,
            "form_type_raw": form_type,
            "amendment_seq": amendment_seq,
            "period_start": period_start,
            "period_end": period_end,
            "is_paper": is_paper,
            "portal_filer_id": portal_filer_id,
        })

    return filings


async def discover_via_portal() -> list[dict]:
    """Discover all filing IDs by scraping the NetFile public portal.

    Strategy: search each DB filer name, get portal filer IDs,
    then scrape each filer's filing page.
    """
    import httpx

    # Get all filer names from DB
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Filer.filer_id, Filer.name).order_by(Filer.name))
        db_filers = result.all()

    logger.info("Searching portal for %d filers...", len(db_filers))

    all_filings = []
    seen_filer_portal_ids = set()
    seen_filing_ids = set()

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        viewstate, viewstate_gen = await _get_portal_viewstate(client)

        # Search for each filer by name
        for i, (filer_id, filer_name) in enumerate(db_filers, 1):
            if _interrupted:
                break

            # Extract search term: last name for "Last, First" format,
            # or first significant word for committee names
            if "," in filer_name:
                search_term = filer_name.split(",")[0].strip()
            else:
                # Skip common words to get more specific results
                words = filer_name.split()
                search_term = words[0] if words else filer_name
                # Skip very short/common first words
                skip_words = {"the", "a", "an", "for", "to", "of", "yes", "no", "committee", "2012", "2014", "2016", "2018", "2020", "2022", "2024", "2026"}
                for w in words:
                    if w.lower() not in skip_words and len(w) > 2:
                        search_term = w
                        break

            if not search_term or len(search_term) < 2:
                continue

            try:
                results = await _search_portal_by_name(client, viewstate, viewstate_gen, search_term)

                # Find the matching portal filer
                for portal_id, portal_name in results:
                    if portal_id in seen_filer_portal_ids:
                        continue
                    # Match by exact or substring
                    if (filer_name.lower() == portal_name.lower() or
                            filer_name.lower() in portal_name.lower() or
                            portal_name.lower() in filer_name.lower()):
                        seen_filer_portal_ids.add(portal_id)

                        # Scrape this filer's filing page
                        filings = await _scrape_filer_filings(client, portal_id)
                        for f in filings:
                            if f["filing_id"] not in seen_filing_ids:
                                f["db_filer_id"] = filer_id
                                all_filings.append(f)
                                seen_filing_ids.add(f["filing_id"])

                        await asyncio.sleep(0.5)
                        break

            except Exception as e:
                logger.debug("Error searching for '%s': %s", search_term, e)

            if i % 20 == 0 or i == len(db_filers):
                print(
                    f"\r  Portal discovery: {i}/{len(db_filers)} filers, "
                    f"{len(seen_filer_portal_ids)} matched, "
                    f"{len(all_filings)} filings "
                    f"{_progress_bar(i, len(db_filers))}",
                    end="", flush=True,
                )

            await asyncio.sleep(0.3)

    print()
    logger.info(
        "Portal discovery: %d filers matched, %d total filings found",
        len(seen_filer_portal_ids), len(all_filings),
    )
    return all_filings


# ─── Step 2: Match Portal Filings to DB ──────────────────────

async def match_and_update_filings(portal_filings: list[dict], dry_run: bool = False) -> dict:
    """Match portal-discovered filings to DB and update IDs / create new records.

    Returns stats dict.
    """
    stats = {"matched": 0, "updated_ids": 0, "new_filings": 0, "skipped_paper": 0}

    async with AsyncSessionLocal() as db:
        # Get all existing filings
        result = await db.execute(select(Filing))
        db_filings = result.scalars().all()

        # Build lookup sets
        existing_netfile_ids = {f.netfile_filing_id for f in db_filings if f.netfile_filing_id}

        # Index composite-key filings by filer_id for matching
        composite_by_filer = defaultdict(list)
        for f in db_filings:
            if _is_composite_key(f.netfile_filing_id):
                composite_by_filer[f.filer_id].append(f)

        for pf in portal_filings:
            portal_filing_id = pf["filing_id"]

            # Already in DB with this ID?
            if portal_filing_id in existing_netfile_ids:
                stats["matched"] += 1
                continue

            # Try to match to a composite-key filing
            db_filer_id = pf.get("db_filer_id")
            if not db_filer_id:
                continue

            matched_db_filing = None
            candidates = composite_by_filer.get(db_filer_id, [])

            for db_filing in candidates:
                if not db_filing.filing_date or not pf["filing_date"]:
                    continue

                # Compare filing date (within 2 days tolerance)
                db_date = db_filing.filing_date
                if hasattr(db_date, 'date'):
                    db_date = db_date.date()
                pf_date = pf["filing_date"]
                if hasattr(pf_date, 'date'):
                    pf_date = pf_date.date()

                try:
                    day_diff = abs((db_date - pf_date).days)
                except TypeError:
                    continue

                if day_diff > 2:
                    continue

                # Also check amendment seq
                if db_filing.amendment_seq is not None and pf["amendment_seq"] is not None:
                    if db_filing.amendment_seq != pf["amendment_seq"]:
                        continue

                matched_db_filing = db_filing
                break

            if matched_db_filing and not dry_run:
                # Update the composite key to the real portal filing ID
                matched_db_filing.netfile_filing_id = portal_filing_id
                matched_db_filing.is_efiled = not pf["is_paper"]
                if pf["form_type"]:
                    matched_db_filing.form_type = pf["form_type"]
                if pf["period_start"]:
                    matched_db_filing.period_start = pf["period_start"]
                if pf["period_end"]:
                    matched_db_filing.period_end = pf["period_end"]
                matched_db_filing.updated_at = datetime.now(timezone.utc)
                # Remove from candidates so it's not matched again
                candidates.remove(matched_db_filing)
                existing_netfile_ids.add(portal_filing_id)
                stats["updated_ids"] += 1
            elif matched_db_filing and dry_run:
                stats["updated_ids"] += 1
            elif not matched_db_filing:
                # New filing not in DB — create it
                if pf["is_paper"]:
                    stats["skipped_paper"] += 1
                    continue

                if not dry_run:
                    new_filing = Filing(
                        netfile_filing_id=portal_filing_id,
                        filer_id=db_filer_id,
                        form_type=pf["form_type"] or "F460",
                        filing_date=pf["filing_date"] or datetime.now(timezone.utc),
                        period_start=pf["period_start"],
                        period_end=pf["period_end"],
                        amendment_seq=pf["amendment_seq"],
                        is_efiled=not pf["is_paper"],
                        data_source="portal_scrape",
                    )
                    db.add(new_filing)
                    existing_netfile_ids.add(portal_filing_id)

                stats["new_filings"] += 1

        if not dry_run:
            await db.commit()

    return stats


# ─── Step 3: Process E-files ─────────────────────────────────

async def process_efiles(
    client: NetFileClient,
    dry_run: bool = False,
    resume_set: set[str] | None = None,
) -> dict:
    """Fetch e-file data for all filings with real API IDs and parse transactions."""
    stats = {
        "filings_processed": 0,
        "new_transactions": 0,
        "skipped_filings": 0,
        "errors": 0,
        "not_efiled": 0,
        "by_schedule": defaultdict(int),
        "by_schedule_amount": defaultdict(float),
    }
    processed_set = resume_set or set()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Filing).where(
                Filing.is_efiled == True,
            ).order_by(Filing.filing_date)
        )
        all_filings = result.scalars().all()

    # Filter to non-composite IDs only
    filings = [
        f for f in all_filings
        if f.netfile_filing_id and not _is_composite_key(f.netfile_filing_id)
    ]
    total = len(filings)
    logger.info("Found %d e-filed filings with real API IDs to process", total)

    for i, filing in enumerate(filings, 1):
        if _interrupted:
            _save_resume_set(processed_set)
            break

        nfid = filing.netfile_filing_id

        if nfid in processed_set:
            stats["skipped_filings"] += 1
            continue

        if dry_run:
            stats["filings_processed"] += 1
            continue

        try:
            zip_bytes = await client.get_efile_data(nfid)

            cal_text = None
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                for name in zf.namelist():
                    if name.lower().endswith(".txt"):
                        cal_text = zf.read(name).decode("utf-8", errors="replace")
                        break

            if not cal_text:
                logger.debug("No CAL text in ZIP for filing %s", nfid)
                processed_set.add(nfid)
                stats["errors"] += 1
                continue

            txn_dicts = parse_cal_transactions(cal_text)

            if not txn_dicts:
                processed_set.add(nfid)
                stats["filings_processed"] += 1
                continue

            # Insert new transactions, skip duplicates
            async with AsyncSessionLocal() as db:
                existing_result = await db.execute(
                    select(Transaction).where(Transaction.filing_id == filing.filing_id)
                )
                existing = existing_result.scalars().all()

                existing_keys = set()
                for ex in existing:
                    key = (
                        (ex.schedule or "").strip(),
                        (ex.entity_name or "").strip().lower(),
                        round(ex.amount, 2) if ex.amount else 0,
                        str(ex.transaction_date) if ex.transaction_date else "",
                    )
                    existing_keys.add(key)

                new_count = 0
                for txn_dict in txn_dicts:
                    dedup_key = (
                        (txn_dict.get("schedule") or "").strip(),
                        (txn_dict.get("entity_name") or "").strip().lower(),
                        round(txn_dict.get("amount", 0), 2),
                        str(txn_dict.get("transaction_date") or ""),
                    )

                    if dedup_key in existing_keys:
                        continue

                    txn = Transaction(
                        filing_id=filing.filing_id,
                        schedule=txn_dict.get("schedule"),
                        transaction_type=txn_dict.get("transaction_type"),
                        transaction_type_code=txn_dict.get("transaction_type_code"),
                        entity_name=txn_dict.get("entity_name"),
                        entity_type=txn_dict.get("entity_type"),
                        first_name=txn_dict.get("first_name"),
                        last_name=txn_dict.get("last_name"),
                        city=txn_dict.get("city"),
                        state=txn_dict.get("state"),
                        zip_code=txn_dict.get("zip_code"),
                        employer=txn_dict.get("employer"),
                        occupation=txn_dict.get("occupation"),
                        amount=txn_dict["amount"],
                        cumulative_amount=txn_dict.get("cumulative_amount"),
                        transaction_date=txn_dict.get("transaction_date"),
                        description=txn_dict.get("description"),
                        memo_code=txn_dict.get("memo_code", False),
                        netfile_transaction_id=txn_dict.get("netfile_transaction_id"),
                        data_source="efile",
                    )
                    db.add(txn)
                    existing_keys.add(dedup_key)
                    new_count += 1

                    schedule = txn_dict.get("schedule") or "?"
                    stats["by_schedule"][schedule] += 1
                    stats["by_schedule_amount"][schedule] += txn_dict.get("amount", 0)

                await db.commit()
                stats["new_transactions"] += new_count
                stats["filings_processed"] += 1

        except Exception as e:
            if "500" in str(e):
                logger.debug("Filing %s not e-filed (HTTP 500)", nfid)
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(Filing).where(Filing.filing_id == filing.filing_id)
                    )
                    f = result.scalar_one_or_none()
                    if f:
                        f.is_efiled = False
                        await db.commit()
                stats["not_efiled"] += 1
            else:
                logger.debug("Error processing filing %s: %s", nfid, e)
                stats["errors"] += 1

        processed_set.add(nfid)
        print(
            f"\r  E-files: {i}/{total} ({stats['new_transactions']} new txns, "
            f"{stats['not_efiled']} paper) "
            f"{_progress_bar(i, total)}",
            end="", flush=True,
        )

        await asyncio.sleep(SCRAPE_RATE_LIMIT)

    print()
    return stats


# ─── Step 4: Download PDFs ───────────────────────────────────

async def download_pdfs(client: NetFileClient, dry_run: bool = False) -> dict:
    """Download PDFs for filings with real API IDs that don't have PDFs yet."""
    stats = {"downloaded": 0, "skipped": 0, "errors": 0}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Filing).where(Filing.pdf_downloaded == False)
        )
        all_filings = result.scalars().all()

    filings = [
        f for f in all_filings
        if f.netfile_filing_id and not _is_composite_key(f.netfile_filing_id)
    ]
    total = len(filings)
    logger.info("Found %d filings without PDFs (with real API IDs)", total)

    if dry_run:
        print(f"  [DRY RUN] Would attempt {total} PDF downloads")
        return stats

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
                stats["downloaded"] += 1
            else:
                stats["errors"] += 1
        except Exception as e:
            logger.debug("PDF download failed for %s: %s", filing.netfile_filing_id, e)
            stats["errors"] += 1

        print(
            f"\r  PDFs: {i}/{total} ({stats['downloaded']} downloaded) "
            f"{_progress_bar(i, total)}",
            end="", flush=True,
        )
        await asyncio.sleep(SCRAPE_RATE_LIMIT)

    print()
    return stats


# ─── Main Pipeline ───────────────────────────────────────────

async def main(args: argparse.Namespace):
    global _interrupted

    import app.models  # noqa: F401
    await init_db()

    client = NetFileClient()
    resume_set = _load_resume_set() if args.resume else set()

    log_id = None
    if not args.dry_run:
        async with AsyncSessionLocal() as db:
            log_entry = ScrapeLog(
                scrape_type="efile_backfill",
                status="running",
                parameters=json.dumps({
                    "dry_run": args.dry_run,
                    "skip_pdfs": args.skip_pdfs,
                    "skip_efiles": args.skip_efiles,
                    "resume": args.resume,
                }),
            )
            db.add(log_entry)
            await db.commit()
            log_id = log_entry.log_id

    efile_stats = {}

    try:
        # ── Step 1: Discover filings via portal scraping ──
        print("\n=== Step 1: Discover Filings via Portal Scraping ===")
        portal_filings = await discover_via_portal()
        print(f"  Total filings discovered: {len(portal_filings)}")

        if portal_filings:
            # ── Step 2: Match and update DB ──
            print("\n=== Step 2: Match Portal Filings to Database ===")
            match_stats = await match_and_update_filings(portal_filings, dry_run=args.dry_run)
            print(f"  Already in DB:      {match_stats['matched']}")
            print(f"  IDs updated:        {match_stats['updated_ids']}")
            print(f"  New filings:        {match_stats['new_filings']}")
            print(f"  Skipped (paper):    {match_stats['skipped_paper']}")
        else:
            print("  No filings discovered. Check logs for errors.")

        # Report current state
        async with AsyncSessionLocal() as db:
            total_filings = (await db.execute(select(func.count(Filing.filing_id)))).scalar() or 0
            real_count = 0
            composite_count = 0
            result = await db.execute(select(Filing.netfile_filing_id))
            for (nfid,) in result.all():
                if nfid and _is_composite_key(nfid):
                    composite_count += 1
                elif nfid:
                    real_count += 1
        print(f"\n  DB state after discovery:")
        print(f"    Total filings: {total_filings}")
        print(f"    Real API IDs:  {real_count}")
        print(f"    Composite IDs: {composite_count}")

        # ── Step 3: Process e-files ──
        if not args.skip_efiles and not _interrupted:
            print("\n=== Step 3: Process E-files ===")
            efile_stats = await process_efiles(
                client,
                dry_run=args.dry_run,
                resume_set=resume_set,
            )
            print(f"\n  Filings processed:  {efile_stats['filings_processed']}")
            print(f"  New transactions:   {efile_stats['new_transactions']}")
            print(f"  Not e-filed:        {efile_stats['not_efiled']}")
            print(f"  Skipped (resume):   {efile_stats['skipped_filings']}")
            print(f"  Errors:             {efile_stats['errors']}")

            if efile_stats["by_schedule"]:
                print("\n  New transactions by schedule:")
                for sched in sorted(efile_stats["by_schedule"].keys()):
                    count = efile_stats["by_schedule"][sched]
                    amount = efile_stats["by_schedule_amount"][sched]
                    print(f"    Schedule {sched}: {count:,} txns (${amount:,.2f})")

        # ── Step 4: Download PDFs ──
        if not args.skip_pdfs and not _interrupted:
            print("\n=== Step 4: Download PDFs ===")
            pdf_stats = await download_pdfs(client, dry_run=args.dry_run)
            print(f"\n  Downloaded: {pdf_stats['downloaded']}")
            print(f"  Errors:     {pdf_stats['errors']}")

        # ── Summary ──
        print("\n" + "=" * 50)
        print("E-FILE BACKFILL COMPLETE")
        print("=" * 50)

        async with AsyncSessionLocal() as db:
            filer_count = (await db.execute(select(func.count()).select_from(Filer))).scalar()
            filing_count = (await db.execute(select(func.count()).select_from(Filing))).scalar()
            txn_count = (await db.execute(select(func.count()).select_from(Transaction))).scalar()
            pdf_count = (await db.execute(
                select(func.count()).select_from(Filing).where(Filing.pdf_downloaded == True)
            )).scalar()

            sched_result = await db.execute(
                select(
                    Transaction.schedule,
                    func.count(Transaction.transaction_id),
                    func.coalesce(func.sum(Transaction.amount), 0),
                ).group_by(Transaction.schedule).order_by(Transaction.schedule)
            )
            sched_rows = sched_result.all()

        print(f"\n  Database totals:")
        print(f"    Filers:        {filer_count:,}")
        print(f"    Filings:       {filing_count:,}")
        print(f"    Transactions:  {txn_count:,}")
        print(f"    PDFs:          {pdf_count:,}")

        print(f"\n  Transactions by schedule:")
        for schedule, count, amount in sched_rows:
            sched_label = schedule or "(none)"
            print(f"    {sched_label:6s}: {count:6,} txns  ${amount:>14,.2f}")

        # Update scrape log
        if log_id and not args.dry_run:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(ScrapeLog).where(ScrapeLog.log_id == log_id)
                )
                log = result.scalar_one()
                log.status = "completed" if not _interrupted else "interrupted"
                log.completed_at = datetime.now(timezone.utc)
                log.items_processed = efile_stats.get("new_transactions", 0)
                await db.commit()

    except Exception as e:
        logger.exception("E-file backfill error: %s", e)
        if log_id and not args.dry_run:
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


def cli():
    parser = argparse.ArgumentParser(
        description="E-file backfill — discover filing IDs via portal scraping, "
                    "parse all CAL schedules, download PDFs."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be done without writing to DB",
    )
    parser.add_argument(
        "--skip-pdfs",
        action="store_true",
        help="Skip PDF downloads",
    )
    parser.add_argument(
        "--skip-efiles",
        action="store_true",
        help="Skip e-file parsing (only do discovery + matching)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume — skip already-processed filings",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = cli()
    asyncio.run(main(args))
