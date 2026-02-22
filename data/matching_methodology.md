# Candidate-Filer Matching Methodology

## Problem

Election candidates from Clarity data have personal names (e.g. "ALLEN LONG"), while campaign finance filers have committee names (e.g. "Allen Long for Supervisor 2024"). The matching algorithm links election candidate records to the correct campaign committee filer records so that filing/transaction data appears on election detail pages.

## Committee Name Patterns

Shasta County committee names typically follow these patterns:

- `{First} {Last} for {Office} {Year}` — most common
- `Committee to Elect {First} {Last} {Office} {Year}`
- `Committee to Re-Elect {First} {Last} {Office} {Year}`
- `Elect {First} {Last} for {Office} {Year}`
- `{Last} for {Office} {Year}` — last name only
- `{Last} 4 {Office} {Year}` — informal
- `{First} {Last} {Office} {Year}` — no preposition

## Matching Algorithm

### Scoring Strategies (in priority order)

| Priority | Strategy | Score | Description |
|----------|----------|-------|-------------|
| 1 | Full name phrase | 95 | Candidate's full name appears as a word-bounded phrase in committee name |
| 2 | All words match | 90 | Every word in candidate name appears as a whole word in committee name |
| 3 | Last name + year | 80 | Last name (whole word) and election year both found in committee name |
| 4 | Last name only | 70 | Last name appears as a whole word in committee name |
| 5 | Fuzzy match | 75 | thefuzz partial_ratio >= 85 between normalized names |

### Disambiguation Rules

- **Year bonus (+5):** Committee names containing the election year get a score boost
- **Recall penalty (-20):** Committee names containing "RECALL" are penalized
- **Minimum threshold:** Score must be >= 70 to accept a match
- **Tie-breaking:** Most filings > most recent filing date

### Filtering

- Measure votes (YES, NO, BONDS) are skipped entirely
- Last names shorter than 3 characters are skipped (avoids false positives from "MA", "LI", etc.)
- Common titles and prepositions (JR, SR, FOR, THE, OF) are stripped from name words

### Word Boundary Matching

All name matching uses word-boundary regex (`\b...\b`) to prevent false positives:
- "LONG" matches "Allen Long for Supervisor" but NOT "Alongside"
- "ALLEN" matches "Allen Long" but NOT "Gallery"
- "RON" matches "Ron Jones" but NOT "Baron Browning"

## Known Edge Cases

| Candidate | Committee | Issue | Resolution |
|-----------|-----------|-------|------------|
| CORKEY HARMON | Corky Harmon District 3 Supervisor 2024 | Spelling variant (CORKEY vs Corky) | Matched via last_name_plus_year (score 80) |
| RON BROWN | Baron Browning for Supervisor 2022 | False positive via fuzzy match | Accepted at score 75 — may need manual correction |
| DIANE ALLEN | Cathy Darling Allen for Clerk 2022 | Different first name, same last | Matched via last_name_plus_year — may be incorrect |
| RONALD A. ANDERSON | Dan Gallier for Anderson City Council 2022 | "ANDERSON" is a city name | False positive — "Anderson" in committee is city name |
| Statewide candidates | N/A | FIONA MA, TRAVIS ALLEN, etc. | No local filings — correctly unmatched after filtering |

## Data Flow

1. **Election ingest** creates ElectionCandidate records linked to minimal filer records (personal names)
2. **Relink script** finds candidates whose filer has 0 filings
3. **Matcher** scores each candidate against filers that DO have filings
4. **Best match** is applied — candidate's filer_id is updated
5. **Orphan cleanup** deletes the old personal-name filer records that are no longer referenced

## Usage

```bash
# Dry run — preview matches
python scripts/relink_candidates.py

# Apply re-links
python scripts/relink_candidates.py --apply

# Apply + cleanup orphan filers
python scripts/relink_candidates.py --apply --clean
```

The matcher is also used by the Settings UI "Check Elections" background task for ongoing automated matching.
