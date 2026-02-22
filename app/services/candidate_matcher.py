"""Candidate-to-filer matching service.

Matches election candidate personal names (e.g. "ALLEN LONG") to campaign
committee filer names (e.g. "Allen Long for Supervisor 2024") using a
multi-strategy scoring system.

Used by both the relink CLI and the Settings UI background tasks.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import ElectionCandidate, Filer, Filing

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    """Result of matching a candidate to a filer."""
    candidate_id: str
    candidate_name: str
    election_year: int
    matched_filer_id: str | None = None
    matched_filer_name: str | None = None
    score: int = 0
    method: str = ""
    filing_count: int = 0


def _normalize(name: str) -> str:
    """Normalize a name for comparison: uppercase, collapse whitespace."""
    return re.sub(r"\s+", " ", name.upper().strip())


def _name_words(name: str) -> list[str]:
    """Split a normalized name into meaningful words (length > 1), drop suffixes/titles."""
    skip = {"JR", "SR", "II", "III", "IV", "MR", "MRS", "MS", "DR", "FOR", "THE", "OF", "AND"}
    return [w for w in _normalize(name).split() if len(w) > 1 and w not in skip]


def _word_in_text(word: str, text: str) -> bool:
    """Check if word appears as a whole word in text (word-boundary match)."""
    return bool(re.search(r'\b' + re.escape(word) + r'\b', text))


def _all_words_in_text(words: list[str], text: str) -> bool:
    """Check if all words appear as whole words in text."""
    return all(_word_in_text(w, text) for w in words)


def _phrase_in_text(phrase: str, text: str) -> bool:
    """Check if phrase appears in text with word boundaries on ends."""
    return bool(re.search(r'\b' + re.escape(phrase) + r'\b', text))


def match_candidate_to_filer(
    candidate_name: str,
    election_year: int,
    filers: list[dict],
) -> MatchResult | None:
    """Match a candidate name to the best filer.

    Args:
        candidate_name: Personal name like "ALLEN LONG"
        election_year: Election year for disambiguation
        filers: List of dicts with keys: filer_id, name, filing_count, last_filing_date

    Returns:
        MatchResult if score >= 70, else None.

    Matching priority:
        1. Full candidate name contained in committee name (score: 95)
        2. All name words found in committee name (score: 90)
        3. Last name + election year in committee name (score: 80)
        4. Last name only in committee name (score: 70)
        5. Fuzzy partial match via thefuzz >= 85 (score: 75)
    """
    if not candidate_name or not filers:
        return None

    cand_norm = _normalize(candidate_name)
    cand_words = _name_words(candidate_name)
    if not cand_words:
        return None

    # Skip measure votes (YES, NO, BONDS) — they're not matchable to filers
    if cand_norm in ("YES", "NO", "BONDS", "BONDS - YES", "BONDS - NO"):
        return None

    last_name = cand_words[-1]
    year_str = str(election_year)

    # Require last name to be at least 3 chars to avoid false positives
    if len(last_name) < 3:
        return None

    candidates_list: list[tuple[int, dict, str]] = []  # (score, filer, method)

    for filer in filers:
        filer_norm = _normalize(filer["name"])
        score = 0
        method = ""

        # Strategy 1: Full candidate name as word-bounded phrase in committee name
        if _phrase_in_text(cand_norm, filer_norm):
            score = 95
            method = "full_name_in_committee"

        # Strategy 2: All name words found as whole words in committee name
        elif _all_words_in_text(cand_words, filer_norm):
            score = 90
            method = "all_words_in_committee"

        # Strategy 3: Last name (whole word) + election year in committee name
        elif _word_in_text(last_name, filer_norm) and year_str in filer_norm:
            score = 80
            method = "last_name_plus_year"

        # Strategy 4: Last name only (whole word) in committee name
        elif _word_in_text(last_name, filer_norm):
            score = 70
            method = "last_name_only"

        # Strategy 5: Fuzzy matching
        else:
            try:
                from thefuzz import fuzz
                ratio = fuzz.partial_ratio(cand_norm, filer_norm)
                if ratio >= 85:
                    score = 75
                    method = f"fuzzy_{ratio}"
            except ImportError:
                pass

        if score < 70:
            continue

        # Disambiguation: prefer committee names containing the election year
        if year_str in filer_norm and method != "last_name_plus_year":
            score += 5

        # Penalize recall committees
        if "RECALL" in filer_norm:
            score -= 20

        if score >= 70:
            candidates_list.append((score, filer, method))

    if not candidates_list:
        return None

    # Sort by: score desc, filing_count desc, last_filing_date desc
    candidates_list.sort(key=lambda x: (
        x[0],
        x[1].get("filing_count", 0),
        x[1].get("last_filing_date", "") or "",
    ), reverse=True)

    best_score, best_filer, best_method = candidates_list[0]

    return MatchResult(
        candidate_id="",  # caller fills in
        candidate_name=candidate_name,
        election_year=election_year,
        matched_filer_id=best_filer["filer_id"],
        matched_filer_name=best_filer["name"],
        score=best_score,
        method=best_method,
        filing_count=best_filer.get("filing_count", 0),
    )


async def find_unlinked_candidates(db: AsyncSession) -> list[ElectionCandidate]:
    """Find election candidates whose linked filer has 0 filings.

    These are candidates that were created during election ingest with
    personal names but need to be re-linked to the actual committee filer.
    """
    # Subquery: filer IDs that have at least one filing
    filers_with_filings = (
        select(Filing.filer_id).distinct()
    ).subquery()

    result = await db.execute(
        select(ElectionCandidate)
        .options(joinedload(ElectionCandidate.filer))
        .options(joinedload(ElectionCandidate.election))
        .where(
            ElectionCandidate.filer_id.notin_(
                select(filers_with_filings.c.filer_id)
            )
        )
    )
    return result.unique().scalars().all()


async def _get_filers_with_filings(db: AsyncSession) -> list[dict]:
    """Get all filers that have at least one filing, with filing counts."""
    result = await db.execute(
        select(
            Filer.filer_id,
            Filer.name,
            func.count(Filing.filing_id).label("filing_count"),
            func.max(Filing.filing_date).label("last_filing_date"),
        )
        .join(Filing, Filing.filer_id == Filer.filer_id)
        .group_by(Filer.filer_id, Filer.name)
    )
    return [
        {
            "filer_id": row.filer_id,
            "name": row.name,
            "filing_count": row.filing_count,
            "last_filing_date": str(row.last_filing_date) if row.last_filing_date else "",
        }
        for row in result.all()
    ]


async def relink_candidates(
    db: AsyncSession,
    dry_run: bool = True,
) -> dict:
    """Re-link unlinked election candidates to filers with filings.

    Returns summary dict with counts and match details.
    """
    unlinked = await find_unlinked_candidates(db)
    filers = await _get_filers_with_filings(db)

    logger.info("Found %d unlinked candidates, %d filers with filings",
                len(unlinked), len(filers))

    matches = []
    no_match = []
    old_filer_ids = set()

    for ec in unlinked:
        if not ec.candidate_name:
            no_match.append(ec)
            continue

        # Get election year from the election relationship
        year = ec.election.year if ec.election else 2024

        result = match_candidate_to_filer(ec.candidate_name, year, filers)

        if result:
            result.candidate_id = ec.id
            matches.append((ec, result))
            old_filer_ids.add(ec.filer_id)
        else:
            no_match.append(ec)

    logger.info("Matched %d candidates, %d unmatched", len(matches), len(no_match))

    if not dry_run and matches:
        for ec, result in matches:
            old_name = ec.filer.name if ec.filer else "?"
            ec.filer_id = result.matched_filer_id
            logger.info(
                "  Re-linked: %s -> %s (was: %s, score: %d, method: %s)",
                ec.candidate_name, result.matched_filer_name,
                old_name, result.score, result.method,
            )
        await db.commit()

    return {
        "total_unlinked": len(unlinked),
        "matched": len(matches),
        "unmatched": len(no_match),
        "old_filer_ids": old_filer_ids,
        "details": [
            {
                "candidate": r.candidate_name,
                "year": r.election_year,
                "matched_to": r.matched_filer_name,
                "score": r.score,
                "method": r.method,
                "filings": r.filing_count,
            }
            for _, r in matches
        ],
        "unmatched_details": [
            ec.candidate_name or ec.filer.name
            for ec in no_match
        ],
    }


async def cleanup_orphan_filers(db: AsyncSession, old_filer_ids: set[str] | None = None) -> int:
    """Delete filer records that have no filings and no election candidate links.

    If old_filer_ids is provided, only consider those filers.
    Returns count deleted.
    """
    # Filers with filings
    filers_with_filings = select(Filing.filer_id).distinct().subquery()
    # Filers with election candidates
    filers_with_candidates = select(ElectionCandidate.filer_id).distinct().subquery()

    query = (
        select(Filer)
        .where(
            Filer.filer_id.notin_(select(filers_with_filings.c.filer_id)),
            Filer.filer_id.notin_(select(filers_with_candidates.c.filer_id)),
        )
    )

    if old_filer_ids:
        query = query.where(Filer.filer_id.in_(old_filer_ids))

    result = await db.execute(query)
    orphans = result.scalars().all()

    for filer in orphans:
        logger.info("  Deleting orphan filer: %s", filer.name)
        await db.delete(filer)

    if orphans:
        await db.commit()

    return len(orphans)
