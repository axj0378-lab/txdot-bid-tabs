"""Stage 6: the statewide master unit-price sheet.

Rows are bid items (keyed by item code); each project adds a column block
headed "<Airport> (<Month Year>)" holding the LOW bidder's unit price and qty.
Re-running for a project already in the sheet updates it instead of
duplicating it.
"""

from pathlib import Path

from .config import MASTER_SHEET_PATH
from .models import Project


def upsert_project(project: Project, path: Path = MASTER_SHEET_PATH) -> Path:
    raise NotImplementedError
