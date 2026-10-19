"""Stage 3: parse a bid-tab PDF into a Project.

PDF layout (new format, Oct 2026+):
  page 1          summary sheet: airport name, scope, CSJ, bidders + base bids
  then per bidder (4 pages each, in bid-opening order, NOT price order):
    page 1 of 4   bid form: "Name of Bidder"
    page 2 of 4   bid tab: Item, Qty, Unit, Description, Unit Price, Total
"""

from pathlib import Path

from .models import ProjectListing, Project


def extract_project(pdf_path: Path, listing: ProjectListing) -> Project:
    """Read the PDF and return the structured project data."""
    raise NotImplementedError


def validate(project: Project) -> list[str]:
    """Return a list of problems found (empty list means the data checks out).

    Checks: qty x unit price == line total, line totals sum to the base bid,
    and every bidder priced the same items.
    """
    raise NotImplementedError
