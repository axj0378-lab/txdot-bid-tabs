"""Stage 1-2: navigate TxDOT's site with Playwright and download project PDFs."""

from pathlib import Path

from .config import BID_RESULTS_URL, DOWNLOAD_DIR
from .models import ProjectListing


def list_projects(full_path: bool = False) -> list[ProjectListing]:
    """Open the bid results page and return every project row in the table.

    full_path=True starts at the Aviation Division home page and clicks
    through the same links shown in the instructions deck; otherwise it goes
    straight to BID_RESULTS_URL.
    """
    raise NotImplementedError


def download_pdf(listing: ProjectListing, dest_dir: Path = DOWNLOAD_DIR) -> Path:
    """Download one project's bid-tab PDF and return its local path."""
    raise NotImplementedError
