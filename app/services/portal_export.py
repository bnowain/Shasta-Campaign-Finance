"""NetFile public portal session — downloads bulk Excel exports.

The NetFile portal (https://public.netfile.com/pub2/?AID=CSHA) is an
ASP.NET WebForms app. To trigger the Excel export we must:
1. GET the portal page to obtain __VIEWSTATE tokens
2. POST with __EVENTTARGET set to the "Export Amended" button
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from app.config import NETFILE_PORTAL_URL, EXPORT_STORAGE_PATH

logger = logging.getLogger(__name__)

# ASP.NET hidden fields we need to echo back
_FORM_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")

# Export button event target
_EXPORT_TARGET = "ctl00$phBody$GetExcelAmend"


class PortalSession:
    """Wraps an httpx.AsyncClient with cookie jar for the ASP.NET portal."""

    def __init__(self, portal_url: str = NETFILE_PORTAL_URL):
        self.portal_url = portal_url
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=120.0,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) netfile-tracker/1.0",
                },
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _extract_form_state(self, html: str) -> tuple[dict, str]:
        """Parse ASP.NET hidden fields and year dropdown field name.

        Returns (form_data_dict, year_field_name).
        """
        soup = BeautifulSoup(html, "lxml")

        form_data = {}
        for field_name in _FORM_FIELDS:
            tag = soup.find("input", {"name": field_name})
            if tag:
                form_data[field_name] = tag.get("value", "")
            else:
                logger.warning("Missing form field: %s", field_name)

        # Find the year dropdown — it's a <select> whose options contain 4-digit years
        year_field = ""
        for select_tag in soup.find_all("select"):
            options = select_tag.find_all("option")
            if options and re.match(r"^\d{4}$", options[0].text.strip()):
                year_field = select_tag.get("name", "")
                break

        if not year_field:
            logger.warning("Could not locate year dropdown in portal HTML")

        return form_data, year_field

    async def download_year_export(self, year: int) -> Path:
        """Download the amended Excel export for a given year.

        Returns the Path to the saved .xlsx file.
        """
        client = await self._get_client()
        dest = EXPORT_STORAGE_PATH / f"CSHA_{year}_amended.xlsx"

        # Step 1: GET portal page to obtain tokens
        logger.info("Fetching portal page for year %d...", year)
        resp = await client.get(self.portal_url)
        resp.raise_for_status()

        form_data, year_field = self._extract_form_state(resp.text)

        if not form_data.get("__VIEWSTATE"):
            raise RuntimeError("Failed to extract __VIEWSTATE from portal page")

        # Step 2: POST with export trigger
        post_data = {
            **form_data,
            "__EVENTTARGET": _EXPORT_TARGET,
            "__EVENTARGUMENT": "",
        }
        if year_field:
            post_data[year_field] = str(year)

        logger.info("Requesting Excel export for %d...", year)
        resp = await client.post(
            self.portal_url,
            data=post_data,
            timeout=180.0,
        )
        resp.raise_for_status()

        # Verify we got a spreadsheet back
        content_type = resp.headers.get("content-type", "")
        if "spreadsheet" not in content_type and "excel" not in content_type and "octet-stream" not in content_type:
            # Sometimes the portal returns HTML on error
            if "text/html" in content_type:
                raise RuntimeError(
                    f"Portal returned HTML instead of Excel for year {year}. "
                    "The year may have no data or the portal layout changed."
                )

        dest.write_bytes(resp.content)
        size_kb = len(resp.content) / 1024
        logger.info("Saved %s (%.1f KB)", dest.name, size_kb)

        return dest

    async def download_range(self, start_year: int, end_year: int, delay: float = 5.0) -> list[Path]:
        """Download exports for a range of years with delay between requests.

        Returns list of Paths to saved files.
        """
        paths = []
        for year in range(start_year, end_year + 1):
            try:
                path = await self.download_year_export(year)
                paths.append(path)
            except Exception as e:
                logger.error("Failed to download year %d: %s", year, e)

            if year < end_year:
                await asyncio.sleep(delay)

        return paths
