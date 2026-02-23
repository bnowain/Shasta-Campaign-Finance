"""People linker CLI — batch-link filers and transactions to Person records.

Usage:
  python scripts/link_people.py              # Dry run (show what would happen)
  python scripts/link_people.py --apply      # Apply links
  python scripts/link_people.py --apply --min-confidence 0.85
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
from app.services.people_linker import (
    link_filers_to_people,
    link_unlinked_transactions,
    cluster_transaction_names,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("link_people")


def _progress(current, total, phase):
    pct = int(current / total * 100) if total else 0
    print(f"\r  [{pct:3d}%] {phase} ({current}/{total})", end="", flush=True)


async def main():
    parser = argparse.ArgumentParser(
        description="Batch-link filers and transactions to Person records"
    )
    parser.add_argument("--apply", action="store_true",
                        help="Apply links (default is dry run)")
    parser.add_argument("--min-confidence", type=float, default=0.80,
                        help="Minimum confidence to create links (default: 0.80)")
    parser.add_argument("--cluster-only", action="store_true",
                        help="Only show name clusters, don't link")
    args = parser.parse_args()

    await init_db()

    mode = "APPLYING" if args.apply else "DRY RUN"
    logger.info("=== People Linker (%s) ===", mode)
    logger.info("Min confidence: %.2f", args.min_confidence)

    async with AsyncSessionLocal() as db:
        if args.cluster_only:
            print("\n  Clustering transaction entity names...")
            clusters = await cluster_transaction_names(db)
            print(f"\n  Found {len(clusters)} name clusters:")
            for canonical, variants in sorted(clusters.items()):
                if len(variants) > 1:
                    print(f"\n    {canonical}:")
                    for v in variants:
                        print(f"      - {v}")
            single = sum(1 for v in clusters.values() if len(v) == 1)
            multi = sum(1 for v in clusters.values() if len(v) > 1)
            print(f"\n  {multi} multi-name clusters, {single} single-name entries")
            return

        # Step 1: Link filers to people
        print("\n  Step 1: Linking filers to people...")
        if args.apply:
            filer_summary = await link_filers_to_people(db, progress_cb=_progress)
        else:
            # Dry run — just report what would happen
            from sqlalchemy import select, func
            from app.models import Filer, Filing, FilerPerson, ElectionCandidate

            filers_with_filings = (await db.execute(
                select(func.count(Filer.filer_id)).where(
                    Filer.filer_id.in_(select(Filing.filer_id).distinct())
                )
            )).scalar() or 0

            existing_filer_links = (await db.execute(
                select(func.count(FilerPerson.id))
            )).scalar() or 0

            candidates_count = (await db.execute(
                select(func.count(ElectionCandidate.id)).where(
                    ElectionCandidate.candidate_name.isnot(None),
                    ElectionCandidate.candidate_name != "",
                )
            )).scalar() or 0

            filer_summary = {
                "filers_linked": filers_with_filings - existing_filer_links,
                "people_created": 0,
                "candidates_linked": candidates_count,
                "flagged_review": 0,
            }

        print()
        print(f"\n{'='*60}")
        print(f"  Filer Linking Summary:")
        print(f"  Filers linked:      {filer_summary['filers_linked']}")
        print(f"  People created:     {filer_summary['people_created']}")
        print(f"  Candidates linked:  {filer_summary['candidates_linked']}")
        print(f"  Flagged for review: {filer_summary['flagged_review']}")
        print(f"{'='*60}")

        # Step 2: Link transactions to people
        print("\n  Step 2: Linking transactions to people...")
        if args.apply:
            txn_summary = await link_unlinked_transactions(
                db,
                progress_cb=_progress,
                min_confidence=args.min_confidence,
            )
        else:
            from app.models import Transaction, TransactionPerson
            total_txns = (await db.execute(
                select(func.count(Transaction.transaction_id)).where(
                    Transaction.entity_name.isnot(None),
                    Transaction.entity_name != "",
                )
            )).scalar() or 0

            linked_txns = (await db.execute(
                select(func.count(TransactionPerson.id))
            )).scalar() or 0

            txn_summary = {
                "linked": 0,
                "created_people": 0,
                "flagged_review": 0,
                "skipped": 0,
                "total": total_txns - linked_txns,
            }

        print()
        print(f"\n{'='*60}")
        print(f"  Transaction Linking Summary:")
        print(f"  Total unlinked:     {txn_summary['total']}")
        print(f"  Linked:             {txn_summary['linked']}")
        print(f"  People created:     {txn_summary['created_people']}")
        print(f"  Flagged for review: {txn_summary['flagged_review']}")
        print(f"  Skipped:            {txn_summary['skipped']}")
        print(f"{'='*60}")

        if not args.apply:
            print(f"\n  (Dry run — use --apply to save changes)")


if __name__ == "__main__":
    asyncio.run(main())
