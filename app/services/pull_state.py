"""In-memory SSE state for the Pull pipeline.

Singleton PullStateManager tracks current pull status and emits
state dicts for SSE streaming to the frontend progress UI.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class PullState:
    """Snapshot of current pull operation state."""

    status: str = "idle"  # idle|discovering|discovered|ingesting|complete|error
    current: int = 0
    total: int = 0
    filing_name: str = ""
    phase: str = ""  # metadata|pdf|efile|transactions
    percent: int = 0
    message: str = ""
    # Summary (populated on complete)
    filings_ingested: int = 0
    pdfs_downloaded: int = 0
    transactions_created: int = 0
    # Error
    error_message: str = ""
    # Timing
    started_at: float = 0.0
    elapsed: float = 0.0


class PullStateManager:
    """Manages pull state and provides SSE-ready snapshots."""

    def __init__(self):
        self._state = PullState()

    def get_current(self) -> dict:
        """Return current state as a dict for SSE serialization."""
        s = self._state
        if s.started_at and s.status in ("discovering", "ingesting"):
            s.elapsed = round(time.time() - s.started_at, 1)
        return {
            "status": s.status,
            "current": s.current,
            "total": s.total,
            "filing_name": s.filing_name,
            "phase": s.phase,
            "percent": s.percent,
            "message": s.message,
            "filings_ingested": s.filings_ingested,
            "pdfs_downloaded": s.pdfs_downloaded,
            "transactions_created": s.transactions_created,
            "error_message": s.error_message,
            "elapsed": s.elapsed,
        }

    def start_timer(self):
        self._state.started_at = time.time()

    def set_discovering(self):
        self._state = PullState(
            status="discovering",
            message="Checking RSS feed for new filings...",
            started_at=self._state.started_at or time.time(),
        )

    def set_discovered(self, count: int):
        self._state.status = "discovered"
        self._state.total = count
        self._state.message = f"Found {count} new filing{'s' if count != 1 else ''}"

    def set_ingesting(self, current: int, total: int, name: str, phase: str):
        self._state.status = "ingesting"
        self._state.current = current
        self._state.total = total
        self._state.filing_name = name
        self._state.phase = phase
        self._state.percent = int((current - 1) / total * 100) if total else 0
        self._state.message = f"[{current}/{total}] {phase}: {name}"

    def set_complete(self, filings: int, pdfs: int, txns: int):
        elapsed = round(time.time() - self._state.started_at, 1) if self._state.started_at else 0
        self._state.status = "complete"
        self._state.percent = 100
        self._state.filings_ingested = filings
        self._state.pdfs_downloaded = pdfs
        self._state.transactions_created = txns
        self._state.elapsed = elapsed
        self._state.message = f"Done: {filings} filings, {pdfs} PDFs, {txns} transactions"

    def set_error(self, msg: str):
        self._state.status = "error"
        self._state.error_message = msg
        self._state.message = f"Error: {msg}"

    def set_idle(self):
        self._state = PullState()


# Module-level singleton
pull_state = PullStateManager()
