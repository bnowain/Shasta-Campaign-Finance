"""Download Clarity Elections result files to local storage.

Reads URLs from data/elections/clarity_links.json and organizes
them into per-election subdirectories under data/elections/clarity/.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import httpx

logger = logging.getLogger("clarity_downloader")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LINKS_PATH = BASE_DIR / "data" / "elections" / "clarity_links.json"
CLARITY_DIR = BASE_DIR / "data" / "elections" / "clarity"

DOWNLOAD_DELAY = 2.0  # seconds between downloads


@dataclass
class DownloadResult:
    election_name: str
    slug: str
    files_downloaded: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# Hardcoded slugs for elections with ambiguous/missing date info
_SLUG_OVERRIDES = {
    "november 7,2023, special election": "2023_1107_special_election",
    "city of shasta lake special vacancy": "2023_0307_shasta_lake_special_vacancy",
    "supervisor district 2 recall": "2022_0201_supervisor_district_2_recall",
    "ca gubernatorial recall election": "2021_0914_ca_gubernatorial_recall",
}


def _election_slug(name: str) -> str:
    """Convert election name to filesystem-safe slug with date prefix.

    Examples:
      "Presidential General November 5, 2024" -> "2024_1105_presidential_general"
      "2022 General Election"                  -> "2022_general_election"
    """
    # Check hardcoded overrides first
    override = _SLUG_OVERRIDES.get(name.lower().strip())
    if override:
        return override

    name_lower = name.lower().strip()

    # Try to extract year
    year_match = re.search(r'\b(20\d{2})\b', name)
    year = year_match.group(1) if year_match else "0000"

    # Try to extract month/day from common patterns
    month_day = ""
    # "November 5, 2024" / "March 5, 2024"
    date_match = re.search(
        r'(january|february|march|april|may|june|july|august|september|'
        r'october|november|december)\s+(\d{1,2})(?:st|nd|rd|th)?',
        name_lower,
    )
    if date_match:
        months = {
            'january': '01', 'february': '02', 'march': '03', 'april': '04',
            'may': '05', 'june': '06', 'july': '07', 'august': '08',
            'september': '09', 'october': '10', 'november': '11', 'december': '12',
        }
        month_day = months[date_match.group(1)] + date_match.group(2).zfill(2)

    # Build clean name part (remove dates, punctuation)
    clean = re.sub(r'[^a-z0-9\s]', '', name_lower)
    # Remove month names
    for m in ['january', 'february', 'march', 'april', 'may', 'june',
              'july', 'august', 'september', 'october', 'november', 'december']:
        clean = re.sub(r'\b' + m + r'\b', '', clean)
    clean = re.sub(r'\b\d{1,2}(?:st|nd|rd|th)?\b', '', clean)  # remove day numbers
    clean = re.sub(r'\b20\d{2}\b', '', clean)   # remove year
    clean = '_'.join(clean.split())
    clean = re.sub(r'_+', '_', clean).strip('_')

    if month_day:
        return f"{year}_{month_day}_{clean}"
    return f"{year}_{clean}"


def _filename_from_url(url: str) -> str:
    """Extract a clean filename from a Clarity URL."""
    # URL-decode and take the last path segment
    decoded = unquote(url)
    name = decoded.rstrip('/').rsplit('/', 1)[-1]

    # Some URLs point to directories (no extension) — skip or use .html
    if '.' not in name:
        name = name + ".html"

    # Sanitize for filesystem
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    # Collapse whitespace
    name = re.sub(r'\s+', '_', name)
    return name


def load_links() -> dict[str, list[dict]]:
    """Load clarity_links.json."""
    if not LINKS_PATH.exists():
        logger.error("Links file not found: %s", LINKS_PATH)
        return {}
    with open(LINKS_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


async def download_election_files(
    election_name: str,
    links: list[dict],
    client: httpx.AsyncClient,
) -> DownloadResult:
    """Download all files for a single election."""
    slug = _election_slug(election_name)
    result = DownloadResult(election_name=election_name, slug=slug)

    dest_dir = CLARITY_DIR / slug
    dest_dir.mkdir(parents=True, exist_ok=True)

    for link in links:
        url = link.get("href", "")
        text = link.get("text", "")
        if not url:
            continue

        filename = _filename_from_url(url)
        dest_path = dest_dir / filename

        # Skip if already downloaded
        if dest_path.exists() and dest_path.stat().st_size > 0:
            result.files_skipped.append(filename)
            logger.debug("  Skip (exists): %s", filename)
            continue

        try:
            logger.info("  Downloading: %s -> %s", text or filename, filename)
            resp = await client.get(url, follow_redirects=True, timeout=60.0)
            resp.raise_for_status()

            dest_path.write_bytes(resp.content)
            result.files_downloaded.append(filename)
            logger.info("  OK: %s (%s bytes)", filename, f"{len(resp.content):,}")

            # Rate limit
            await asyncio.sleep(DOWNLOAD_DELAY)

        except Exception as e:
            error_msg = f"{filename}: {e}"
            result.errors.append(error_msg)
            logger.warning("  FAIL: %s", error_msg)

    return result


async def download_all(
    election_filter: str | None = None,
) -> list[DownloadResult]:
    """Download files for all elections (or a filtered subset).

    Args:
        election_filter: Optional substring to match election names.
    """
    links_data = load_links()
    if not links_data:
        return []

    results: list[DownloadResult] = []
    CLARITY_DIR.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(
        headers={"User-Agent": "ShastaElectionTracker/1.0"},
    ) as client:
        for election_name, links in links_data.items():
            if election_filter and election_filter.lower() not in election_name.lower():
                continue

            logger.info("Election: %s (%d files)", election_name, len(links))
            result = await download_election_files(election_name, links, client)
            results.append(result)

            total = len(result.files_downloaded) + len(result.files_skipped)
            logger.info(
                "  Done: %d downloaded, %d skipped, %d errors (of %d total)",
                len(result.files_downloaded),
                len(result.files_skipped),
                len(result.errors),
                total,
            )

    return results


def get_election_files(slug: str) -> list[dict]:
    """List downloaded files for an election slug.

    Returns list of dicts with 'filename', 'size', 'ext' keys.
    """
    election_dir = CLARITY_DIR / slug
    if not election_dir.exists():
        return []

    files = []
    for f in sorted(election_dir.iterdir()):
        if f.is_file():
            files.append({
                "filename": f.name,
                "size": f.stat().st_size,
                "ext": f.suffix.lower(),
            })
    return files


def list_election_slugs() -> list[str]:
    """List all election slugs that have downloaded files."""
    if not CLARITY_DIR.exists():
        return []
    return sorted(
        d.name for d in CLARITY_DIR.iterdir()
        if d.is_dir() and any(d.iterdir())
    )
