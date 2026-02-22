"""Candidate re-linking CLI — fix election candidate-to-filer mappings.

Matches election candidate personal names to committee filer records
that actually have filings.

Usage:
  python scripts/relink_candidates.py              # Dry run (show matches)
  python scripts/relink_candidates.py --apply      # Apply re-links
  python scripts/relink_candidates.py --apply --clean  # + remove orphan filers
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import AsyncSessionLocal, init_db
from app.services.candidate_matcher import relink_candidates, cleanup_orphan_filers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("relink_candidates")


async def main():
    parser = argparse.ArgumentParser(
        description="Re-link election candidates to filers with filings"
    )
    parser.add_argument("--apply", action="store_true",
                        help="Apply re-links (default is dry run)")
    parser.add_argument("--clean", action="store_true",
                        help="Also remove orphan filers after re-linking")
    args = parser.parse_args()

    await init_db()

    async with AsyncSessionLocal() as db:
        dry_run = not args.apply
        mode = "DRY RUN" if dry_run else "APPLYING"
        logger.info("=== Candidate Re-link (%s) ===", mode)

        summary = await relink_candidates(db, dry_run=dry_run)

        print(f"\n{'='*60}")
        print(f"  Total unlinked candidates: {summary['total_unlinked']}")
        print(f"  Matched:                   {summary['matched']}")
        print(f"  Unmatched:                 {summary['unmatched']}")
        print(f"{'='*60}")

        if summary["details"]:
            print(f"\n  Matches:")
            for d in summary["details"]:
                print(f"    {d['candidate']} ({d['year']}) -> {d['matched_to']} "
                      f"[score={d['score']}, {d['method']}, {d['filings']} filings]")

        if summary["unmatched_details"]:
            print(f"\n  Unmatched candidates:")
            for name in summary["unmatched_details"][:20]:
                print(f"    {name}")
            if len(summary["unmatched_details"]) > 20:
                print(f"    ... and {len(summary['unmatched_details']) - 20} more")

        if dry_run:
            print(f"\n  (Dry run — use --apply to save changes)")
        else:
            print(f"\n  Changes applied.")

        # Cleanup orphans
        if args.clean and args.apply:
            print(f"\n  Cleaning up orphan filers...")
            deleted = await cleanup_orphan_filers(db, summary.get("old_filer_ids"))
            print(f"  Deleted {deleted} orphan filer(s).")
        elif args.clean and not args.apply:
            print(f"\n  (Skipping orphan cleanup — requires --apply)")


if __name__ == "__main__":
    asyncio.run(main())
