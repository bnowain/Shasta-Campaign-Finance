"""Central configuration — paths, ports, env vars, auto-create dirs."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# NetFile API
NETFILE_AID = os.getenv("NETFILE_AID", "CSHA")
NETFILE_API_BASE = os.getenv("NETFILE_API_BASE", "https://netfile.com/Connect2/api")
NETFILE_PORTAL_URL = os.getenv("NETFILE_PORTAL_URL", "https://public.netfile.com/pub2/?AID=CSHA")

# Application
APP_PORT = int(os.getenv("APP_PORT", "8855"))
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")

# Paths
DATABASE_PATH = BASE_DIR / os.getenv("DATABASE_PATH", "database/netfile_tracker.db")
PDF_STORAGE_PATH = BASE_DIR / os.getenv("PDF_STORAGE_PATH", "pdfs")
EXPORT_STORAGE_PATH = BASE_DIR / os.getenv("EXPORT_STORAGE_PATH", "exports")
LOG_DIR = BASE_DIR / "logs"

DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"

# Scraper
SCRAPE_RATE_LIMIT = float(os.getenv("SCRAPE_RATE_LIMIT", "2.0"))
PDF_DOWNLOAD_DELAY = float(os.getenv("PDF_DOWNLOAD_DELAY", "3.0"))
RSS_POLL_INTERVAL = int(os.getenv("RSS_POLL_INTERVAL", "1800"))

# Atlas Hub
ATLAS_HUB_URL = os.getenv("ATLAS_HUB_URL", "http://localhost:8800")
ATLAS_SPOKE_NAME = os.getenv("ATLAS_SPOKE_NAME", "netfile-tracker")

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = BASE_DIR / os.getenv("LOG_FILE", "logs/netfile_tracker.log")

# Ensure all directories exist at import time
for _d in [DATABASE_PATH.parent, PDF_STORAGE_PATH, EXPORT_STORAGE_PATH, LOG_DIR]:
    _d.mkdir(parents=True, exist_ok=True)
