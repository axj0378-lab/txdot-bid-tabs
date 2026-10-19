"""Stages 4-5: per-project bid summary as Excel and PDF.

Layout: item columns (Item, Qty, Unit, Description) followed by one
Unit Price / Total column pair per bidder, company name above each pair,
with the low bidder highlighted.
"""

from pathlib import Path

from .config import OUTPUT_DIR
from .models import Project


def write_summary_excel(project: Project, out_dir: Path = OUTPUT_DIR) -> Path:
    raise NotImplementedError


def write_summary_pdf(project: Project, out_dir: Path = OUTPUT_DIR) -> Path:
    raise NotImplementedError
