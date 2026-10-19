"""Central configuration: URLs, paths, and run settings."""

from datetime import date
from pathlib import Path

# --- Paths -----------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = ROOT_DIR / "downloads"
OUTPUT_DIR = ROOT_DIR / "output"
MASTER_SHEET_PATH = OUTPUT_DIR / "master_unit_prices.xlsx"

# --- TxDOT navigation --------------------------------------------------------
# Full click-path start: the Aviation Division page (slides 1-2 of the deck).
AVIATION_DIVISION_URL = "https://www.txdot.gov/about/divisions/aviation-division.html"
# Direct start at the bid results table (slide 6).
BID_RESULTS_URL = "https://www.dot.state.tx.us/AVN/avninfo/status/construct/index.htm"

# Link text clicked along the full path (matched case-insensitively).
NAV_LINK_RESOURCES = "Aviation Resources and Opportunities"
NAV_LINK_BID_RESULTS = "Bid results"

# TxDOT switched to the new bid-tab PDF format in October 2026.
# Projects opened before this date use the old layout and are skipped.
NEW_FORMAT_START = date(2026, 10, 1)

# --- Browser settings --------------------------------------------------------
HEADLESS = True
NAV_TIMEOUT_MS = 30_000
