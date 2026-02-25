"""System endpoints — health check, port status, zombie cleanup, shutdown."""

import asyncio
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter

from app.config import APP_PORT, NETFILE_AID, ATLAS_SPOKE_NAME
from app.utils.process_manager import get_port_status, kill_port

router = APIRouter(prefix="/api", tags=["system"])
logger = logging.getLogger(__name__)


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": ATLAS_SPOKE_NAME,
        "port": APP_PORT,
        "agency": NETFILE_AID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/system/port-status")
async def port_status():
    """Diagnostic info about port usage."""
    return get_port_status(APP_PORT)


@router.post("/system/kill-zombies")
async def kill_zombies():
    """Kill zombie processes on the app port."""
    result = kill_port(APP_PORT)
    return result


@router.post("/system/shutdown")
async def system_shutdown():
    """Gracefully shut down the server."""
    logger.info("Shutdown requested, exiting in 500ms")

    async def _exit_soon():
        await asyncio.sleep(0.5)
        os._exit(0)

    asyncio.get_event_loop().create_task(_exit_soon())
    return {"status": "shutting_down", "killed": []}
