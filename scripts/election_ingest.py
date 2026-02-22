"""Election ingest CLI — scrape portal tree + import vote results.

Usage:
  python scripts/election_ingest.py                          # Phase A: portal scrape
  python scripts/election_ingest.py --phase-b --file X.csv   # Import single CSV
  python scripts/election_ingest.py --phase-b --dir data/elections/  # Import all CSVs
  python scripts/election_ingest.py --phase-c                # Phase C: Clarity download + parse
  python scripts/election_ingest.py --phase-c --download-only  # Just download files
  python scripts/election_ingest.py --phase-c --parse-only     # Parse already-downloaded files
  python scripts/election_ingest.py --phase-c --election "2024 General"  # Single election
  python scripts/election_ingest.py --list                   # Show elections in DB
  python scripts/election_ingest.py --years 2020-2024        # Limit year range
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, func
from sqlalchemy.orm import joinedload, selectinload

from app.db import AsyncSessionLocal, init_db
from app.models import Election, ElectionCandidate, Filer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("election_ingest")


# ─── Phase A: Portal Tree Scrape ──────────────────────────────

async def phase_a(min_year: int = 2016, max_year: int = 2026):
    """Scrape NetFile portal tree and upsert Election + ElectionCandidate records."""
    from app.services.election_scraper import scrape_elections_sync

    logger.info("Phase A: Scraping NetFile portal tree (%d-%d)...", min_year, max_year)
    scraped = scrape_elections_sync(min_year=min_year, max_year=max_year)

    logger.info("Scraped %d elections, upserting to database...", len(scraped))

    async with AsyncSessionLocal() as session:
        # Load existing filers for matching
        filers = (await session.execute(select(Filer))).scalars().all()
        filer_by_netfile_id = {f.netfile_filer_id: f for f in filers if f.netfile_filer_id}
        filer_by_local_id = {f.local_filer_id: f for f in filers if f.local_filer_id}
        filer_by_name_lower = {}
        for f in filers:
            filer_by_name_lower.setdefault(f.name.lower().strip(), []).append(f)
        logger.info("Loaded %d existing filers for matching", len(filers))

        elections_created = 0
        elections_updated = 0
        candidates_created = 0
        candidates_skipped = 0

        for se in scraped:
            # Upsert election
            existing = (await session.execute(
                select(Election).where(Election.netfile_election_id == se.node_value)
            )).scalars().first()

            if existing:
                election = existing
                elections_updated += 1
            else:
                election = Election(
                    date=se.date,
                    name=se.name,
                    election_type=se.election_type,
                    year=se.year,
                    netfile_election_id=se.node_value,
                    data_source="netfile_portal",
                )
                session.add(election)
                await session.flush()
                elections_created += 1

            # Upsert candidates
            for sc in se.candidates:
                # Match candidate to existing filer
                filer = _match_filer(
                    sc.portal_filer_id, sc.name,
                    filer_by_netfile_id, filer_by_local_id, filer_by_name_lower,
                )

                if not filer:
                    # Create a minimal filer record
                    filer = Filer(
                        netfile_filer_id=sc.portal_filer_id,
                        name=sc.name,
                        filer_type="candidate",
                        office=sc.office,
                    )
                    session.add(filer)
                    await session.flush()
                    # Add to lookup dicts
                    if filer.netfile_filer_id:
                        filer_by_netfile_id[filer.netfile_filer_id] = filer
                    filer_by_name_lower.setdefault(filer.name.lower().strip(), []).append(filer)
                    logger.debug("Created new filer: %s (portal_id=%s)", sc.name, sc.portal_filer_id)

                # Check if candidate link already exists
                existing_ec = (await session.execute(
                    select(ElectionCandidate).where(
                        ElectionCandidate.election_id == election.election_id,
                        ElectionCandidate.filer_id == filer.filer_id,
                    )
                )).scalars().first()

                if existing_ec:
                    # Update office if missing
                    if not existing_ec.office_sought and sc.office:
                        existing_ec.office_sought = sc.office
                    candidates_skipped += 1
                else:
                    ec = ElectionCandidate(
                        election_id=election.election_id,
                        filer_id=filer.filer_id,
                        office_sought=sc.office,
                        candidate_name=sc.name,
                    )
                    session.add(ec)
                    candidates_created += 1

        await session.commit()

    logger.info("Phase A complete: %d elections created, %d updated, "
                "%d candidates linked, %d skipped (already existed)",
                elections_created, elections_updated,
                candidates_created, candidates_skipped)


def _match_filer(
    portal_id: str | None,
    name: str,
    by_netfile_id: dict,
    by_local_id: dict,
    by_name_lower: dict,
) -> Filer | None:
    """Match a scraped candidate to an existing Filer record."""
    # 1. Exact match by NetFile portal ID
    if portal_id and portal_id in by_netfile_id:
        return by_netfile_id[portal_id]

    # 2. Exact match by name (case-insensitive)
    key = name.lower().strip()
    matches = by_name_lower.get(key, [])
    if len(matches) == 1:
        return matches[0]

    # 3. Try fuzzy matching if thefuzz is available
    try:
        from thefuzz import fuzz
        best_score = 0
        best_filer = None
        for filer_name_lower, filer_list in by_name_lower.items():
            score = fuzz.ratio(key, filer_name_lower)
            if score > best_score and score >= 85:
                best_score = score
                best_filer = filer_list[0]
        if best_filer:
            logger.debug("Fuzzy match: '%s' -> '%s' (score=%d)",
                         name, best_filer.name, best_score)
            return best_filer
    except ImportError:
        pass

    return None


# ─── Phase B: CSV Vote Result Import ─────────────────────────

async def phase_b(csv_path: str | None = None, csv_dir: str | None = None,
                  result_source: str = "county_csv"):
    """Import vote results from CSV files."""
    from app.services.election_csv_parser import parse_election_csv

    paths: list[Path] = []
    if csv_path:
        paths.append(Path(csv_path))
    elif csv_dir:
        d = Path(csv_dir)
        paths.extend(sorted(d.glob("*.csv")))
    else:
        logger.error("Phase B requires --file or --dir")
        return

    if not paths:
        logger.warning("No CSV files found")
        return

    logger.info("Phase B: Importing %d CSV file(s)...", len(paths))

    async with AsyncSessionLocal() as session:
        # Load elections + filers for matching
        elections = (await session.execute(select(Election))).scalars().all()
        filers = (await session.execute(select(Filer))).scalars().all()

        election_by_date = {}
        for e in elections:
            date_str = e.date.strftime("%m/%d/%Y") if e.date else ""
            election_by_date[date_str] = e
            # Also index by ISO format
            iso_str = e.date.isoformat() if e.date else ""
            election_by_date[iso_str] = e

        filer_by_name_lower = {}
        for f in filers:
            filer_by_name_lower.setdefault(f.name.lower().strip(), []).append(f)

        total_updated = 0
        total_skipped = 0

        for csv_file in paths:
            logger.info("Processing: %s", csv_file.name)
            rows = parse_election_csv(csv_file)

            for row in rows:
                # Find the election
                election = None
                if row.election_date:
                    election = election_by_date.get(row.election_date)

                if not election:
                    logger.warning("  No matching election for date '%s' (candidate: %s)",
                                   row.election_date, row.candidate_name)
                    total_skipped += 1
                    continue

                # Find the candidate's filer
                filer = _match_filer(
                    None, row.candidate_name,
                    {}, {}, filer_by_name_lower,
                )

                if not filer:
                    logger.warning("  No matching filer for '%s'", row.candidate_name)
                    total_skipped += 1
                    continue

                # Find or create ElectionCandidate record
                ec = (await session.execute(
                    select(ElectionCandidate).where(
                        ElectionCandidate.election_id == election.election_id,
                        ElectionCandidate.filer_id == filer.filer_id,
                    )
                )).scalars().first()

                if not ec:
                    ec = ElectionCandidate(
                        election_id=election.election_id,
                        filer_id=filer.filer_id,
                        office_sought=row.office,
                        candidate_name=row.candidate_name,
                    )
                    session.add(ec)
                    await session.flush()

                # Update vote result fields
                if row.votes is not None:
                    ec.votes_received = row.votes
                if row.vote_pct is not None:
                    ec.vote_percentage = row.vote_pct
                if row.is_winner is not None:
                    ec.is_winner = row.is_winner
                if row.incumbent is not None:
                    ec.incumbent = row.incumbent
                if row.party:
                    ec.party = row.party
                if row.result_notes:
                    ec.result_notes = row.result_notes
                if not ec.office_sought and row.office:
                    ec.office_sought = row.office

                ec.result_source = result_source
                total_updated += 1

        await session.commit()

    logger.info("Phase B complete: %d results updated, %d skipped",
                total_updated, total_skipped)


# ─── Phase C: Clarity Elections Download + Parse ─────────────

# Mapping from Clarity election names to approximate election dates.
# Used to match downloaded results to Election records in the DB.
CLARITY_DATE_MAP = {
    "november 4th, 2025 statewide special election": "2025-11-04",
    "presidential general november 5, 2024": "2024-11-05",
    "presidential primary march 5, 2024": "2024-03-05",
    "november 7,2023, special election": "2023-11-07",
    "city of shasta lake special vacancy": "2023-03-07",
    "2022 general election": "2022-11-08",
    "2022 statewide direct primary election": "2022-06-07",
    "supervisor district 2 recall": "2022-02-01",
    "ca gubernatorial recall election": "2021-09-14",
    "2020 presidential general": "2020-11-03",
    "2020 presidential primary": "2020-03-03",
    "2018 general election": "2018-11-06",
    "2018 primary election": "2018-06-05",
    "2016 general election": "2016-11-08",
    "2016 primary election": "2016-06-07",
}

# Files that contain parseable structured vote data
PARSEABLE_PATTERNS = [
    # CSV formats (2022-2023)
    "CVR_Export", "Detailed_vote_totals", "Generic_ENR_Export",
    # Excel formats (2024, 2020, 2021, 2018)
    "Official_Canvass_Results", "Cumulative_Results", "Final_Official_Results",
    "District_Total_Canvas", "Precinct_Canvas",
    "StatementOfVotesCast", "StatementOfVotes",
    "SOVDistrict",
    "Statement_of_Vote_Precinct_Detail",
]


def _find_parseable_files(election_dir: Path) -> list[Path]:
    """Find parseable CSV/XLSX files in an election directory."""
    parseable = []
    for f in election_dir.iterdir():
        if not f.is_file():
            continue
        suffix = f.suffix.lower()
        if suffix not in ('.csv', '.xlsx'):
            continue
        # Check if filename matches a known parseable pattern
        fname = f.stem
        for pattern in PARSEABLE_PATTERNS:
            if pattern.lower().replace('_', '') in fname.lower().replace('_', '').replace(' ', '').replace('-', ''):
                parseable.append(f)
                break
    return parseable


def _match_candidate_name(clarity_name: str, ec_name: str) -> int:
    """Score how well a Clarity candidate name matches an ElectionCandidate name.

    Returns 0-100 score. Higher = better match.
    """
    # Normalize both names
    a = clarity_name.upper().strip()
    b = ec_name.upper().strip()

    # Exact match
    if a == b:
        return 100

    # Remove common prefixes/suffixes
    for prefix in ("DR. ", "DR ", "MR. ", "MR ", "MRS. ", "MRS ", "MS. ", "MS "):
        a = a.removeprefix(prefix)
        b = b.removeprefix(prefix)

    if a == b:
        return 95

    # Check if last name matches (strongest signal for Shasta local races)
    a_parts = a.split()
    b_parts = b.split()
    if a_parts and b_parts and a_parts[-1] == b_parts[-1]:
        # Last name match — check first name too
        if len(a_parts) > 1 and len(b_parts) > 1:
            if a_parts[0] == b_parts[0]:
                return 90  # First + last match
            if a_parts[0][0] == b_parts[0][0]:
                return 80  # First initial + last match
        return 75  # Last name only

    # Check if one name contains the other
    if a in b or b in a:
        return 70

    # Try fuzzy matching
    try:
        from thefuzz import fuzz
        return fuzz.ratio(a, b)
    except ImportError:
        pass

    return 0


async def phase_c(
    download_only: bool = False,
    parse_only: bool = False,
    election_filter: str | None = None,
):
    """Phase C: Download Clarity files + parse structured results into DB."""
    from datetime import date as date_type

    from app.services.clarity_downloader import download_all, CLARITY_DIR, load_links
    from app.services.clarity_parser import parse_file

    # Step 1: Download files
    if not parse_only:
        logger.info("Phase C: Downloading Clarity Elections files...")
        results = await download_all(election_filter=election_filter)

        total_downloaded = sum(len(r.files_downloaded) for r in results)
        total_skipped = sum(len(r.files_skipped) for r in results)
        total_errors = sum(len(r.errors) for r in results)
        logger.info(
            "Download complete: %d downloaded, %d skipped, %d errors across %d elections",
            total_downloaded, total_skipped, total_errors, len(results),
        )

        if download_only:
            return

    # Step 2: Parse structured files and import to DB
    logger.info("Phase C: Parsing structured files and importing results...")

    links_data = load_links()
    if not CLARITY_DIR.exists():
        logger.error("Clarity directory not found: %s", CLARITY_DIR)
        return

    async with AsyncSessionLocal() as session:
        # Load all elections for date matching
        all_elections = (await session.execute(
            select(Election).order_by(Election.date.desc())
        )).scalars().all()

        election_by_date: dict[str, Election] = {}
        for e in all_elections:
            if e.date:
                election_by_date[e.date.isoformat()] = e

        # Load all election candidates for matching (joinedload for async-safe filer access)
        all_candidates = (await session.execute(
            select(ElectionCandidate).options(joinedload(ElectionCandidate.filer))
        )).unique().scalars().all()

        # Group candidates by election_id
        candidates_by_election: dict[str, list[ElectionCandidate]] = {}
        for ec in all_candidates:
            candidates_by_election.setdefault(ec.election_id, []).append(ec)

        # Local filer name cache to avoid duplicate creation within this run
        filer_cache: dict[str, Filer] = {}

        elections_processed = 0
        candidates_matched = 0
        candidates_created = 0
        results_updated = 0

        for election_name, links in links_data.items():
            if election_filter and election_filter.lower() not in election_name.lower():
                continue

            # Find the matching directory
            from app.services.clarity_downloader import _election_slug
            slug = _election_slug(election_name)
            election_dir = CLARITY_DIR / slug

            if not election_dir.exists():
                logger.debug("No downloaded files for: %s", election_name)
                continue

            # Find matching Election record by date
            date_str = CLARITY_DATE_MAP.get(election_name.lower().strip())
            if not date_str:
                logger.warning("No date mapping for election: %s", election_name)
                continue

            election = election_by_date.get(date_str)
            if not election:
                # Auto-create election record
                from datetime import date as date_cls
                parts = date_str.split("-")
                election_date = date_cls(int(parts[0]), int(parts[1]), int(parts[2]))

                # Determine election type
                name_lower = election_name.lower()
                if "primary" in name_lower:
                    etype = "primary"
                elif "general" in name_lower:
                    etype = "general"
                elif "special" in name_lower or "recall" in name_lower or "vacancy" in name_lower:
                    etype = "special"
                else:
                    etype = "general"

                election = Election(
                    date=election_date,
                    name=election_name,
                    election_type=etype,
                    year=election_date.year,
                    data_source="clarity",
                )
                session.add(election)
                await session.flush()
                election_by_date[date_str] = election
                logger.info("Created election: %s (%s)", election_name, date_str)

            # Store source URL from HTML results link
            for link in links:
                href = link.get("href", "")
                text = link.get("text", "").lower()
                if "election results" in text or "local election results" in text:
                    if not election.source_url:
                        election.source_url = href

            # Find parseable files
            parseable = _find_parseable_files(election_dir)
            if not parseable:
                logger.info("No parseable files for: %s (slug=%s)", election_name, slug)
                continue

            logger.info("Processing: %s (%d parseable files)", election_name, len(parseable))

            # Parse each file — use the first one that returns results
            all_races = []
            for fpath in parseable:
                try:
                    races = parse_file(fpath)
                    if races:
                        logger.info("  Parsed %d races from: %s", len(races), fpath.name)
                        all_races.extend(races)
                        break  # Use first successful parse
                except Exception as e:
                    logger.warning("  Failed to parse %s: %s", fpath.name, e)

            if not all_races:
                logger.warning("  No races parsed for: %s", election_name)
                continue

            # Get existing candidates for this election
            existing_candidates = candidates_by_election.get(election.election_id, [])

            # Update election-level stats from the first race
            if all_races[0].registered_voters and not election.total_registered:
                election.total_registered = all_races[0].registered_voters
            if all_races[0].ballots_cast and not election.total_ballots_cast:
                election.total_ballots_cast = all_races[0].ballots_cast
            if all_races[0].turnout_pct and not election.turnout_percentage:
                election.turnout_percentage = all_races[0].turnout_pct

            # Process each race
            for race in all_races:
                for cand in race.candidates:
                    # Try to match to existing ElectionCandidate
                    best_match = None
                    best_score = 0

                    for ec in existing_candidates:
                        # Match by candidate_name or filer name
                        names_to_check = [ec.candidate_name]
                        if ec.filer and hasattr(ec.filer, 'name'):
                            names_to_check.append(ec.filer.name)

                        for check_name in names_to_check:
                            if not check_name:
                                continue
                            score = _match_candidate_name(cand.name, check_name)
                            if score > best_score:
                                best_score = score
                                best_match = ec

                    if best_match and best_score >= 70:
                        # Update existing record
                        best_match.votes_received = cand.votes
                        best_match.vote_percentage = cand.vote_pct
                        best_match.result_source = "clarity"
                        if cand.party and not best_match.party:
                            best_match.party = cand.party
                        if not best_match.office_sought and race.contest_name:
                            best_match.office_sought = race.contest_name
                        candidates_matched += 1
                        results_updated += 1
                    else:
                        # Create new ElectionCandidate record
                        # First, find or create a filer by exact name
                        cache_key = cand.name.lower().strip()
                        filer_result = filer_cache.get(cache_key)

                        if not filer_result:
                            filer_result = (await session.execute(
                                select(Filer).where(
                                    func.lower(Filer.name) == cache_key
                                )
                            )).scalars().first()

                        if not filer_result:
                            filer_result = Filer(
                                name=cand.name,
                                filer_type="candidate" if not race.is_measure else "measure",
                                office=race.contest_name if not race.is_measure else None,
                            )
                            session.add(filer_result)
                            await session.flush()

                        filer_cache[cache_key] = filer_result

                        # Check if this election+filer combo already exists
                        existing_ec = (await session.execute(
                            select(ElectionCandidate).where(
                                ElectionCandidate.election_id == election.election_id,
                                ElectionCandidate.filer_id == filer_result.filer_id,
                            )
                        )).scalars().first()

                        if existing_ec:
                            # Update the existing record
                            existing_ec.votes_received = cand.votes
                            existing_ec.vote_percentage = cand.vote_pct
                            existing_ec.result_source = "clarity"
                            if cand.party and not existing_ec.party:
                                existing_ec.party = cand.party
                            if not existing_ec.office_sought:
                                existing_ec.office_sought = race.contest_name
                            candidates_matched += 1
                            results_updated += 1
                        else:
                            new_ec = ElectionCandidate(
                                election_id=election.election_id,
                                filer=filer_result,
                                office_sought=race.contest_name,
                                candidate_name=cand.name,
                                party=cand.party,
                                votes_received=cand.votes,
                                vote_percentage=cand.vote_pct,
                                result_source="clarity",
                                is_measure=race.is_measure,
                            )

                            # For YES/NO on measures
                            if race.is_measure and cand.name.upper() in ("YES", "NO"):
                                new_ec.position = "support" if cand.name.upper() == "YES" else "oppose"

                            session.add(new_ec)
                            await session.flush()
                            candidates_created += 1
                            results_updated += 1

                            # Add to lookup for future matching within this run
                            existing_candidates.append(new_ec)

                # Determine winners and finish positions for this race
                # Reload candidates for this race from existing_candidates
                race_candidates = [
                    ec for ec in existing_candidates
                    if ec.votes_received is not None
                    and ec.result_source == "clarity"
                    and ec.office_sought == race.contest_name
                ]

                if race_candidates:
                    race_candidates.sort(key=lambda ec: -(ec.votes_received or 0))
                    for pos, ec in enumerate(race_candidates, 1):
                        ec.finish_position = pos
                        if pos == 1 and not race.is_measure:
                            ec.is_winner = True
                        elif pos == 1 and race.is_measure:
                            # For measures, YES wins if it has more votes
                            if ec.candidate_name and ec.candidate_name.upper() == "YES":
                                ec.is_winner = True
                            elif ec.position == "support":
                                ec.is_winner = True

            # Mark election as having clarity results
            election.data_source = "clarity"
            election.results_certified = True
            elections_processed += 1

        await session.commit()

    logger.info(
        "Phase C complete: %d elections processed, %d candidates matched, "
        "%d new candidates created, %d results updated",
        elections_processed, candidates_matched, candidates_created, results_updated,
    )


# ─── List Elections ───────────────────────────────────────────

async def list_elections():
    """Print elections in the database."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Election).order_by(Election.date.desc())
        )
        elections = result.scalars().all()

        if not elections:
            print("No elections in database.")
            return

        print(f"\n{'Date':<14} {'Type':<10} {'Name':<45} {'Source':<15}")
        print("-" * 85)

        for e in elections:
            # Count candidates
            count = (await session.execute(
                select(func.count(ElectionCandidate.id)).where(
                    ElectionCandidate.election_id == e.election_id
                )
            )).scalar() or 0

            date_str = e.date.strftime("%Y-%m-%d") if e.date else "N/A"
            print(f"{date_str:<14} {(e.election_type or 'N/A'):<10} "
                  f"{e.name[:44]:<45} {(e.data_source or 'N/A'):<15} "
                  f"({count} candidates)")

        print(f"\nTotal: {len(elections)} elections")


# ─── CLI ──────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Election data ingest — portal scrape + CSV/Clarity import"
    )
    parser.add_argument("--phase-b", action="store_true",
                        help="Run Phase B (CSV vote result import)")
    parser.add_argument("--phase-c", action="store_true",
                        help="Run Phase C (Clarity download + parse)")
    parser.add_argument("--file", type=str,
                        help="Single CSV file to import (Phase B)")
    parser.add_argument("--dir", type=str,
                        help="Directory of CSV files to import (Phase B)")
    parser.add_argument("--source", type=str, default="county_csv",
                        choices=["county_csv", "county_pdf", "manual"],
                        help="Result source tag (default: county_csv)")
    parser.add_argument("--download-only", action="store_true",
                        help="Phase C: only download files, skip parsing")
    parser.add_argument("--parse-only", action="store_true",
                        help="Phase C: only parse already-downloaded files")
    parser.add_argument("--election", type=str,
                        help="Phase C: filter to single election name substring")
    parser.add_argument("--list", action="store_true",
                        help="List elections in database")
    parser.add_argument("--years", type=str, default="2016-2026",
                        help="Year range for Phase A (default: 2016-2026)")
    return parser.parse_args()


async def main():
    args = parse_args()
    await init_db()

    if args.list:
        await list_elections()
        return

    # Parse year range
    parts = args.years.split("-")
    min_year = int(parts[0])
    max_year = int(parts[1]) if len(parts) > 1 else min_year

    if args.phase_c:
        await phase_c(
            download_only=args.download_only,
            parse_only=args.parse_only,
            election_filter=args.election,
        )
    elif args.phase_b:
        await phase_b(
            csv_path=args.file,
            csv_dir=args.dir,
            result_source=args.source,
        )
    else:
        # Default: Phase A (portal scrape)
        await phase_a(min_year=min_year, max_year=max_year)


if __name__ == "__main__":
    asyncio.run(main())
