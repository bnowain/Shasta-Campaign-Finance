"""People linker service — name normalization, fuzzy matching, batch auto-linking.

Matches transaction entity names and filer names to Person records using a
multi-tier confidence system:
  - High (>=0.95): auto-link, needs_review=False
  - Medium (0.80–0.95): auto-link, needs_review=True (flagged for human review)
  - Low (<0.80): skip (leave unlinked)
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import (
    Person, Filer, Filing, Transaction,
    FilerPerson, TransactionPerson, ElectionCandidate,
)

logger = logging.getLogger(__name__)

# Patterns that indicate a committee/organization rather than an individual
COMMITTEE_PATTERNS = re.compile(
    r"\b(committee|for\s+supervisor|for\s+council|for\s+mayor|for\s+sheriff|"
    r"for\s+judge|for\s+district|for\s+school|for\s+board|pac\b|party\b|"
    r"republican|democrat|association|coalition|fund|inc\b|llc\b|"
    r"no\s+on|yes\s+on|measure\s+[a-z]|recall)",
    re.IGNORECASE,
)

# Entity type codes for committees
COMMITTEE_TYPE_CODES = {"COM", "OTH", "PTY", "SCC"}


def normalize_entity_name(raw_name: str, entity_type_code: str | None = None) -> str:
    """Normalize an entity name for matching.

    - Individual names in "LAST, FIRST MIDDLE" format → "First Middle Last"
    - Committee names preserved in title case
    - Collapse whitespace, strip
    """
    if not raw_name:
        return ""

    name = raw_name.strip()
    if not name:
        return ""

    # Determine if this is a committee
    is_committee = (
        (entity_type_code and entity_type_code.upper() in COMMITTEE_TYPE_CODES)
        or bool(COMMITTEE_PATTERNS.search(name))
    )

    if not is_committee and "," in name:
        # Try "LAST, FIRST MIDDLE" → "First Middle Last"
        parts = name.split(",", 1)
        last = parts[0].strip()
        first_middle = parts[1].strip() if len(parts) > 1 else ""
        if first_middle and last:
            name = f"{first_middle} {last}"

    # Title-case and collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    name = name.title()

    # Fix common title-case artifacts
    name = re.sub(r"\bLlc\b", "LLC", name)
    name = re.sub(r"\bPac\b", "PAC", name)
    name = re.sub(r"\bInc\b", "Inc.", name)

    return name


def _detect_entity_type(name: str, entity_type_code: str | None = None) -> str:
    """Detect entity type from name patterns."""
    if entity_type_code and entity_type_code.upper() in COMMITTEE_TYPE_CODES:
        return "committee"
    if COMMITTEE_PATTERNS.search(name):
        return "committee"
    return "individual"


async def cluster_transaction_names(db: AsyncSession) -> dict[str, list[str]]:
    """Group distinct transaction entity names by fuzzy similarity.

    Returns dict mapping canonical name → list of raw name variants.
    """
    result = await db.execute(
        select(
            Transaction.entity_name,
            func.count(Transaction.transaction_id).label("cnt"),
        )
        .where(Transaction.entity_name.isnot(None))
        .where(Transaction.entity_name != "")
        .group_by(Transaction.entity_name)
    )
    name_counts = [(row[0], row[1]) for row in result.all()]

    if not name_counts:
        return {}

    try:
        from thefuzz import fuzz
    except ImportError:
        # Without fuzzy matching, each name is its own cluster
        return {normalize_entity_name(n): [n] for n, _ in name_counts}

    # Sort by frequency descending — most common form becomes canonical
    name_counts.sort(key=lambda x: x[1], reverse=True)

    clusters: dict[str, list[str]] = {}  # normalized canonical → raw names
    assigned: set[str] = set()

    for raw_name, count in name_counts:
        if raw_name in assigned:
            continue

        norm = normalize_entity_name(raw_name)
        if not norm:
            continue

        # Check if this name matches an existing cluster
        matched_cluster = None
        for canonical in clusters:
            ratio = fuzz.token_sort_ratio(norm.upper(), canonical.upper())
            if ratio >= 90:
                matched_cluster = canonical
                break

        if matched_cluster:
            clusters[matched_cluster].append(raw_name)
        else:
            clusters[norm] = [raw_name]

        assigned.add(raw_name)

    return clusters


async def match_to_person(
    name: str,
    db: AsyncSession,
    all_people: list[Person] | None = None,
) -> tuple[Person | None, float]:
    """Match a normalized name to an existing Person record.

    Returns (Person, confidence) or (None, 0.0).
    Confidence: 1.0=exact, 0.98=alias match, 0.xx=fuzzy.
    """
    if not name:
        return None, 0.0

    name_upper = name.upper().strip()

    # Load all people if not provided
    if all_people is None:
        all_people = (await db.execute(select(Person))).scalars().all()

    # 1. Exact canonical_name match
    for p in all_people:
        if p.canonical_name and p.canonical_name.upper().strip() == name_upper:
            return p, 1.0

    # 2. Alias match
    for p in all_people:
        if p.aliases:
            try:
                aliases = json.loads(p.aliases) if isinstance(p.aliases, str) else p.aliases
                for alias in aliases:
                    if alias.upper().strip() == name_upper:
                        return p, 0.98
            except (json.JSONDecodeError, TypeError):
                pass

    # 3. Fuzzy match
    try:
        from thefuzz import fuzz
    except ImportError:
        return None, 0.0

    best_match = None
    best_ratio = 0.0

    for p in all_people:
        if not p.canonical_name:
            continue
        ratio = fuzz.token_sort_ratio(name_upper, p.canonical_name.upper()) / 100.0
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = p

    if best_match and best_ratio >= 0.80:
        return best_match, best_ratio

    return None, 0.0


async def link_filers_to_people(
    db: AsyncSession,
    progress_cb=None,
) -> dict:
    """Link election candidates to Person records via FilerPerson.

    Committees/PACs live in the Filer table — they are NOT Person records.
    Only actual humans (candidates, treasurers) get Person records.

    Currently links: ElectionCandidate names → Person (role="candidate").
    Future: Form 460 treasurer names → Person (role="treasurer").

    Returns summary dict.
    """
    summary = {
        "people_created": 0,
        "candidates_linked": 0,
        "flagged_review": 0,
    }

    all_people = list((await db.execute(select(Person))).scalars().all())

    # Link election candidates as individuals
    candidates = (await db.execute(
        select(ElectionCandidate)
        .options(joinedload(ElectionCandidate.filer))
        .options(joinedload(ElectionCandidate.election))
        .where(ElectionCandidate.candidate_name.isnot(None))
        .where(ElectionCandidate.candidate_name != "")
    )).unique().scalars().all()

    total_candidates = len(candidates)
    for i, ec in enumerate(candidates):
        if progress_cb:
            progress_cb(i + 1, total_candidates, "Linking candidates")

        # Skip measure votes
        cand_upper = ec.candidate_name.upper().strip()
        if cand_upper in ("YES", "NO", "BONDS", "BONDS - YES", "BONDS - NO"):
            continue

        # Check if filer already has a candidate-role link
        existing_link = (await db.execute(
            select(FilerPerson).where(
                FilerPerson.filer_id == ec.filer_id,
                FilerPerson.role == "candidate",
            )
        )).scalars().first()

        if existing_link:
            continue

        norm_name = normalize_entity_name(ec.candidate_name)
        if not norm_name:
            continue

        person, confidence = await match_to_person(norm_name, db, all_people)

        if not person:
            person = Person(
                canonical_name=norm_name,
                entity_type="individual",
            )
            db.add(person)
            await db.flush()
            all_people.append(person)
            summary["people_created"] += 1
            confidence = 1.0

        needs_review = 0.80 <= confidence < 0.95
        if needs_review:
            summary["flagged_review"] += 1

        link = FilerPerson(
            filer_id=ec.filer_id,
            person_id=person.person_id,
            role="candidate",
            match_confidence=confidence,
            needs_review=needs_review,
            source="auto",
        )
        db.add(link)
        summary["candidates_linked"] += 1

    await db.commit()
    return summary


async def link_unlinked_transactions(
    db: AsyncSession,
    progress_cb=None,
    min_confidence: float = 0.80,
) -> dict:
    """Link unlinked transactions to Person records.

    Groups transactions by normalized entity_name, matches/creates Person
    records, and creates TransactionPerson junction records.

    Returns summary dict.
    """
    summary = {
        "linked": 0,
        "created_people": 0,
        "flagged_review": 0,
        "skipped": 0,
        "total": 0,
    }

    # Get transaction IDs that already have person links
    linked_txn_ids_q = select(TransactionPerson.transaction_id).distinct()

    # Get unlinked transactions grouped by entity_name
    unlinked = (await db.execute(
        select(
            Transaction.entity_name,
            Transaction.entity_type,
            func.count(Transaction.transaction_id).label("cnt"),
        )
        .where(
            Transaction.entity_name.isnot(None),
            Transaction.entity_name != "",
            Transaction.transaction_id.notin_(linked_txn_ids_q),
        )
        .group_by(Transaction.entity_name, Transaction.entity_type)
    )).all()

    summary["total"] = sum(row[2] for row in unlinked)

    if not unlinked:
        return summary

    all_people = list((await db.execute(select(Person))).scalars().all())
    total_groups = len(unlinked)

    for i, (entity_name, entity_type_code, count) in enumerate(unlinked):
        if progress_cb:
            progress_cb(i + 1, total_groups, "Linking transactions")

        norm_name = normalize_entity_name(entity_name, entity_type_code)
        if not norm_name:
            summary["skipped"] += count
            continue

        # Skip committees/organizations — they live in the Filer table, not Person
        det_type = _detect_entity_type(entity_name, entity_type_code)
        if det_type == "committee":
            summary["skipped"] += count
            continue

        person, confidence = await match_to_person(norm_name, db, all_people)

        if confidence < min_confidence and not person:
            # Below threshold and no exact match — create new individual person
            person = Person(
                canonical_name=norm_name,
                entity_type="individual",
            )
            db.add(person)
            await db.flush()
            all_people.append(person)
            summary["created_people"] += 1
            confidence = 1.0  # New record = exact match
        elif confidence < min_confidence:
            summary["skipped"] += count
            continue

        needs_review = 0.80 <= confidence < 0.95

        # Get all unlinked transaction IDs for this entity_name
        txn_ids_result = await db.execute(
            select(Transaction.transaction_id).where(
                Transaction.entity_name == entity_name,
                Transaction.transaction_id.notin_(linked_txn_ids_q),
            )
        )
        txn_ids = txn_ids_result.scalars().all()

        for txn_id in txn_ids:
            link = TransactionPerson(
                transaction_id=txn_id,
                person_id=person.person_id,
                match_confidence=confidence,
                needs_review=needs_review,
                source="auto",
            )
            db.add(link)
            summary["linked"] += 1
            if needs_review:
                summary["flagged_review"] += 1

        # Add alias if it differs from canonical
        if norm_name != person.canonical_name:
            aliases = []
            if person.aliases:
                try:
                    aliases = json.loads(person.aliases)
                except (json.JSONDecodeError, TypeError):
                    aliases = []
            if norm_name not in aliases:
                aliases.append(norm_name)
                person.aliases = json.dumps(aliases)

    await db.commit()
    return summary
