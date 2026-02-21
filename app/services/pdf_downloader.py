"""PDF download service for filing images.

Downloads rendered PDF images from the NetFile public API
and stores them locally in PDF_STORAGE_PATH.
"""

from __future__ import annotations

import logging

import aiofiles

from app.config import PDF_STORAGE_PATH
from app.services.netfile_api import NetFileClient

logger = logging.getLogger(__name__)


async def download_filing_pdf(
    client: NetFileClient, netfile_filing_id: str
) -> tuple[bool, str, int]:
    """Download a filing PDF to local storage.

    Returns (success, file_path, file_size).
    Skips download if file already exists.
    """
    pdf_path = PDF_STORAGE_PATH / f"{netfile_filing_id}.pdf"

    # Skip if already downloaded
    if pdf_path.exists():
        size = pdf_path.stat().st_size
        logger.info("PDF already exists: %s (%d bytes)", pdf_path.name, size)
        return True, str(pdf_path), size

    try:
        pdf_bytes = await client.get_filing_pdf(netfile_filing_id)
        async with aiofiles.open(str(pdf_path), "wb") as f:
            await f.write(pdf_bytes)
        size = len(pdf_bytes)
        logger.info("Downloaded PDF: %s (%d bytes)", pdf_path.name, size)
        return True, str(pdf_path), size
    except Exception as e:
        logger.error("Failed to download PDF %s: %s", netfile_filing_id, e)
        return False, "", 0
