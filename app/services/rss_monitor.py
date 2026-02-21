"""RSS feed parsing and new-filing discovery.

Fetches the NetFile RSS feed, parses entries, and identifies
filings not yet in the database.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
from sqlalchemy import select

from app.models import Filing, RssFeedState
from app.services.netfile_api import NetFileClient

logger = logging.getLogger(__name__)

# Filing ID is the last path segment of the <link> URL
_FILING_ID_RE = re.compile(r"/(\d+)$")


@dataclass
class DiscoveredFiling:
    """A filing found in RSS that doesn't exist in the DB yet."""

    guid: str
    netfile_filing_id: str
    filer_name: str
    form_description: str
    pdf_url: str


def parse_rss_entries(xml_text: str) -> list[dict]:
    """Parse RSS XML into a list of entry dicts.

    Each dict has: guid, filing_id, filer_name, form_description, pdf_url
    """
    feed = feedparser.parse(xml_text)
    entries = []
    for item in feed.entries:
        guid = getattr(item, "id", "") or ""
        link = getattr(item, "link", "") or ""
        title = getattr(item, "title", "") or ""
        description = getattr(item, "description", "") or ""

        # Extract filing_id from link URL (last numeric segment)
        m = _FILING_ID_RE.search(link)
        if not m:
            logger.warning("Could not extract filing_id from RSS link: %s", link)
            continue

        entries.append({
            "guid": guid,
            "filing_id": m.group(1),
            "filer_name": title,
            "form_description": description,
            "pdf_url": link,
        })
    return entries


async def discover_new_filings(
    client: NetFileClient, db
) -> list[DiscoveredFiling]:
    """Fetch RSS, compare against DB, return list of new filings."""
    xml_text = await client.get_rss_feed()
    entries = parse_rss_entries(xml_text)

    if not entries:
        return []

    # Get existing filing IDs from DB
    filing_ids = [e["filing_id"] for e in entries]
    result = await db.execute(
        select(Filing.netfile_filing_id).where(
            Filing.netfile_filing_id.in_(filing_ids)
        )
    )
    existing_ids = {row[0] for row in result.fetchall()}

    # Also check RssFeedState for last_guid
    feed_state = await db.execute(
        select(RssFeedState).where(RssFeedState.feed_url == "campaign")
    )
    state_row = feed_state.scalar_one_or_none()
    last_guid = state_row.last_guid if state_row else None

    new_filings = []
    for entry in entries:
        # Stop if we hit the last-seen guid
        if entry["guid"] == last_guid:
            break
        # Skip if already in DB
        if entry["filing_id"] in existing_ids:
            continue
        new_filings.append(DiscoveredFiling(
            guid=entry["guid"],
            netfile_filing_id=entry["filing_id"],
            filer_name=entry["filer_name"],
            form_description=entry["form_description"],
            pdf_url=entry["pdf_url"],
        ))

    logger.info("RSS discovery: %d entries, %d new", len(entries), len(new_filings))
    return new_filings


async def update_feed_state(db, latest_guid: str):
    """Update or create the RssFeedState record with latest guid."""
    result = await db.execute(
        select(RssFeedState).where(RssFeedState.feed_url == "campaign")
    )
    state = result.scalar_one_or_none()

    if state:
        state.last_guid = latest_guid
        state.last_polled = datetime.now(timezone.utc)
    else:
        state = RssFeedState(
            feed_url="campaign",
            last_guid=latest_guid,
            last_polled=datetime.now(timezone.utc),
        )
        db.add(state)

    await db.commit()
