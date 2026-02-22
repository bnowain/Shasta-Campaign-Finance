# Election Results CSV Format

Place CSV files in this directory for import via:

```
python scripts/election_ingest.py --phase-b --dir data/elections/
python scripts/election_ingest.py --phase-b --file data/elections/2024_general.csv
```

## Expected Columns

| Column | Required | Aliases | Description |
|--------|----------|---------|-------------|
| election_date | Yes | date | Date in MM/DD/YYYY or YYYY-MM-DD format |
| office | No | race, contest | Race/office name |
| candidate_name | Yes | candidate, contestant, name | Candidate name |
| party | No | party_name | Political party |
| votes | No | votes_received, total_votes | Vote count |
| vote_pct | No | vote_percentage, pct, percent | Vote percentage |
| is_winner | No | winner, won | 1/true/yes/won = winner |
| incumbent | No | is_incumbent | 1/true/yes = incumbent |
| result_notes | No | notes | Free-text notes |

## Example CSV

```csv
election_date,office,candidate_name,party,votes,vote_pct,is_winner,incumbent
11/08/2022,Supervisor District 1,Kevin Crye,,5432,52.1,yes,no
11/08/2022,Supervisor District 1,Erin Resner,,4998,47.9,no,no
```

## PDF Sources

For results originally from PDF documents:
1. OCR the PDF externally (Tesseract, Adobe, etc.)
2. Create a CSV following the format above
3. Store the original PDF in `data/elections/pdfs/`
4. Import with `--source county_pdf`:

```
python scripts/election_ingest.py --phase-b --file data/elections/2023_special.csv --source county_pdf
```

The `result_source` field will be set to `county_pdf` and the election detail page
will show a PDF badge next to those results.
