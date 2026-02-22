"""Flexible CSV parser for election vote results.

Supports various column name aliases since data comes from mixed sources
(county CSVs, manually prepared from PDFs, etc.).

Expected CSV columns (with aliases):
    election_date: date of the election
    office: race/office name
    candidate_name / candidate / contestant: person name
    party: political party
    votes / votes_received: vote count
    vote_pct / vote_percentage / pct: percentage
    is_winner / winner: boolean (1/true/yes/won)
    incumbent: boolean
    result_notes / notes: free text
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Column name aliases (lowercase) -> canonical name
_ALIASES: dict[str, str] = {
    # election_date
    "election_date": "election_date",
    "date": "election_date",
    "electiondate": "election_date",
    "election": "election_date",
    # office
    "office": "office",
    "race": "office",
    "contest": "office",
    "office_name": "office",
    # candidate
    "candidate_name": "candidate_name",
    "candidate": "candidate_name",
    "contestant": "candidate_name",
    "name": "candidate_name",
    # party
    "party": "party",
    "party_name": "party",
    # votes
    "votes": "votes",
    "votes_received": "votes",
    "total_votes": "votes",
    "vote_count": "votes",
    # vote_pct
    "vote_pct": "vote_pct",
    "vote_percentage": "vote_pct",
    "pct": "vote_pct",
    "percent": "vote_pct",
    "percentage": "vote_pct",
    # is_winner
    "is_winner": "is_winner",
    "winner": "is_winner",
    "won": "is_winner",
    # incumbent
    "incumbent": "incumbent",
    "is_incumbent": "incumbent",
    # notes
    "result_notes": "result_notes",
    "notes": "result_notes",
}

_TRUTHY = {"1", "true", "yes", "won", "y", "t", "x"}


@dataclass
class CsvVoteRow:
    """One row of parsed vote result data."""
    election_date: str | None = None
    office: str | None = None
    candidate_name: str | None = None
    party: str | None = None
    votes: int | None = None
    vote_pct: float | None = None
    is_winner: bool | None = None
    incumbent: bool | None = None
    result_notes: str | None = None


def _normalize_header(raw: str) -> str | None:
    """Map a raw column header to a canonical name, or None if unknown."""
    clean = raw.strip().lower().replace(" ", "_").replace("-", "_")
    return _ALIASES.get(clean)


def _parse_bool(val: str) -> bool | None:
    if not val or not val.strip():
        return None
    return val.strip().lower() in _TRUTHY


def _parse_int(val: str) -> int | None:
    if not val or not val.strip():
        return None
    try:
        return int(val.strip().replace(",", ""))
    except ValueError:
        return None


def _parse_float(val: str) -> float | None:
    if not val or not val.strip():
        return None
    try:
        return float(val.strip().replace("%", "").replace(",", ""))
    except ValueError:
        return None


def parse_election_csv(path: Path | str) -> list[CsvVoteRow]:
    """Parse a vote results CSV file.

    Returns list of CsvVoteRow. Skips rows where candidate_name is empty.
    """
    path = Path(path)
    rows: list[CsvVoteRow] = []

    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        # Build column mapping
        col_map: dict[str, str] = {}
        for raw_col in reader.fieldnames or []:
            canonical = _normalize_header(raw_col)
            if canonical:
                col_map[raw_col] = canonical
            else:
                logger.debug("Unknown column '%s' in %s — skipping", raw_col, path.name)

        if "candidate_name" not in col_map.values():
            logger.warning("No candidate_name column found in %s (columns: %s)",
                           path.name, list(reader.fieldnames or []))

        for line_num, raw_row in enumerate(reader, start=2):
            mapped = {col_map[k]: v for k, v in raw_row.items() if k in col_map}

            name = (mapped.get("candidate_name") or "").strip()
            if not name:
                continue

            row = CsvVoteRow(
                election_date=mapped.get("election_date", "").strip() or None,
                office=mapped.get("office", "").strip() or None,
                candidate_name=name,
                party=mapped.get("party", "").strip() or None,
                votes=_parse_int(mapped.get("votes", "")),
                vote_pct=_parse_float(mapped.get("vote_pct", "")),
                is_winner=_parse_bool(mapped.get("is_winner", "")),
                incumbent=_parse_bool(mapped.get("incumbent", "")),
                result_notes=mapped.get("result_notes", "").strip() or None,
            )
            rows.append(row)

    logger.info("Parsed %d candidate rows from %s", len(rows), path.name)
    return rows
