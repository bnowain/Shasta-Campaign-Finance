"""NetFile Connect2 public API client.

Async httpx client wrapping all 6 public endpoints.
No authentication required.
"""

from __future__ import annotations

import httpx

from app.config import NETFILE_API_BASE, NETFILE_AID, SCRAPE_RATE_LIMIT


class NetFileClient:
    """Async client for the NetFile Connect2 public API."""

    def __init__(self, base_url: str = NETFILE_API_BASE, aid: str = NETFILE_AID):
        self.base_url = base_url.rstrip("/")
        self.aid = aid
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=30.0,
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def list_filers(self, page: int = 0, page_size: int = 100) -> dict:
        """List all filers for the agency (paginated).

        POST /api/public/campaign/list/filer
        """
        client = await self._get_client()
        resp = await client.post(
            "/api/public/campaign/list/filer",
            json={
                "aid": self.aid,
                "currentPageIndex": page,
                "pageSize": page_size,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_filing_info(self, filing_id: str) -> dict:
        """Get filing metadata by filing ID.

        POST /api/public/filing/info/{filing_id}
        """
        client = await self._get_client()
        resp = await client.post(
            f"/api/public/filing/info/{filing_id}",
            json={"filingId": filing_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_filing_pdf(self, filing_id: str) -> bytes:
        """Download filing as rendered PDF.

        GET /api/public/image/{filing_id}
        """
        client = await self._get_client()
        resp = await client.get(
            f"/api/public/image/{filing_id}",
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.content

    async def get_efile_data(self, filing_id: str) -> bytes:
        """Get e-filing CAL data as ZIP bytes.

        POST /api/public/efile/{filing_id}
        Returns raw ZIP bytes containing Efile.txt (CAL format).
        Raises httpx.HTTPStatusError on 500 for non-efiled filings.
        """
        client = await self._get_client()
        resp = await client.post(
            f"/api/public/efile/{filing_id}",
            json={"filingId": filing_id},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.content

    async def get_rss_feed(self) -> str:
        """Get RSS feed of recent filings (XML).

        GET /api/public/list/filing/rss/{AID}/campaign.xml
        """
        client = await self._get_client()
        resp = await client.get(
            f"/api/public/list/filing/rss/{self.aid}/campaign.xml",
        )
        resp.raise_for_status()
        return resp.text

    async def list_all_filers(self) -> list[dict]:
        """Paginate through all filers and return the full list."""
        all_filers = []
        page = 0
        while True:
            data = await self.list_filers(page=page, page_size=100)
            results = data.get("filers", [])
            all_filers.extend(results)
            total = data.get("totalMatchingCount", 0)
            if len(all_filers) >= total or not results:
                break
            page += 1
        return all_filers

    async def list_filings(self, page: int = 0, page_size: int = 100) -> dict:
        """List all filings for the agency (paginated).

        POST /api/public/campaign/list/filing
        Same pagination pattern as list_filers.
        """
        client = await self._get_client()
        resp = await client.post(
            "/api/public/campaign/list/filing",
            json={
                "aid": self.aid,
                "currentPageIndex": page,
                "pageSize": page_size,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def list_all_filings(self) -> list[dict]:
        """Paginate through all filings and return the full list."""
        all_filings = []
        page = 0
        while True:
            data = await self.list_filings(page=page, page_size=100)
            # Try common response keys
            results = data.get("filings", data.get("results", []))
            all_filings.extend(results)
            total = data.get("totalMatchingCount", data.get("totalCount", 0))
            if len(all_filings) >= total or not results:
                break
            page += 1
        return all_filings

    async def get_transaction_types(self) -> dict:
        """Get transaction type lookup table.

        POST /api/public/campaign/list/transaction/types
        """
        client = await self._get_client()
        resp = await client.post(
            "/api/public/campaign/list/transaction/types",
            json={},
        )
        resp.raise_for_status()
        return resp.json()
