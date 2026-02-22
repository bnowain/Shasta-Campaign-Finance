"""In-memory SSE state for Settings background tasks.

Singleton SettingsStateManager tracks current task status and emits
state dicts for SSE streaming to the Settings page progress UI.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class SettingsState:
    """Snapshot of current settings task state."""

    status: str = "idle"  # idle|running|complete|error
    task_type: str = ""   # elections|filings
    current: int = 0
    total: int = 0
    percent: int = 0
    phase: str = ""
    message: str = ""
    # Summary (populated on complete)
    filers_synced: int = 0
    elections_found: int = 0
    candidates_linked: int = 0
    filings_discovered: int = 0
    filings_ingested: int = 0
    # Error
    error_message: str = ""
    # Timing
    started_at: float = 0.0
    elapsed: float = 0.0


class SettingsStateManager:
    """Manages settings task state and provides SSE-ready snapshots."""

    def __init__(self):
        self._state = SettingsState()

    def get_current(self) -> dict:
        """Return current state as a dict for SSE serialization."""
        s = self._state
        if s.started_at and s.status == "running":
            s.elapsed = round(time.time() - s.started_at, 1)
        return {
            "status": s.status,
            "task_type": s.task_type,
            "current": s.current,
            "total": s.total,
            "percent": s.percent,
            "phase": s.phase,
            "message": s.message,
            "filers_synced": s.filers_synced,
            "elections_found": s.elections_found,
            "candidates_linked": s.candidates_linked,
            "filings_discovered": s.filings_discovered,
            "filings_ingested": s.filings_ingested,
            "error_message": s.error_message,
            "elapsed": s.elapsed,
        }

    def start(self, task_type: str):
        self._state = SettingsState(
            status="running",
            task_type=task_type,
            started_at=time.time(),
            message=f"Starting {task_type} check...",
        )

    def set_progress(self, current: int, total: int, phase: str, message: str = ""):
        self._state.current = current
        self._state.total = total
        self._state.phase = phase
        self._state.percent = int(current / total * 100) if total else 0
        self._state.message = message or f"{phase} ({current}/{total})"

    def set_complete(self, **kwargs):
        elapsed = round(time.time() - self._state.started_at, 1) if self._state.started_at else 0
        self._state.status = "complete"
        self._state.percent = 100
        self._state.elapsed = elapsed
        for k, v in kwargs.items():
            if hasattr(self._state, k):
                setattr(self._state, k, v)
        self._state.message = "Complete"

    def set_error(self, msg: str):
        self._state.status = "error"
        self._state.error_message = msg
        self._state.message = f"Error: {msg}"

    def set_idle(self):
        self._state = SettingsState()


# Module-level singleton
settings_state = SettingsStateManager()
