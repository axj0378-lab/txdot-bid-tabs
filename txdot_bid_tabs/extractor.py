"""Stage 3: parse a bid-tab PDF into a Project.

PDF layout (new format, Oct 2026+), confirmed on the Lockhart PDF:
  page 1     Bid Result Summary Sheet. A *fillable form*: airport, scope, CSJ,
             date/time/location, bidder names and base bids are form fields, so
             they are read exactly with no OCR.
  then, per bidder, in bid-opening order (NOT price order):
             "Bid Form - Page 1 of 4"  scanned  -> Name of Bidder (OCR)
             "Bid Form - Page 2 of 4"  scanned  -> bid tab table (grid OCR)

Because the bid tabs are OCR'd, every bidder's numbers are cross-checked:
  * qty x unit price must equal the line total,
  * line totals must sum to the base bid typed on the summary form,
  * item codes / quantities must agree across bidders (majority vote).
Anything that can't be reconciled is kept but flagged in Project.notes.
"""

from __future__ import annotations

import difflib
import itertools
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader

from . import ocr
from .models import Bid, BidItem, Project, ProjectListing

log = logging.getLogger(__name__)

CENT = 0.005


# --------------------------------------------------------------------------
# Summary sheet (fillable form on page 1)
# --------------------------------------------------------------------------

def read_summary_form(pdf_path: Path) -> dict:
    """Read the page-1 form fields. Returns {} if the PDF has no form."""
    fields = PdfReader(str(pdf_path)).get_fields() or {}
    values = {k: (v.get("/V") or "") for k, v in fields.items()}

    def get(prefix: str) -> str:
        for k, v in values.items():
            if k.startswith(prefix) and isinstance(v, str):
                return v.strip()
        return ""

    bidders: dict[int, str] = {}
    base_bids: dict[int, float] = {}
    for k, v in values.items():
        if m := re.fullmatch(r"Bidder1\.(\d+)", k):
            if str(v).strip():
                bidders[int(m.group(1))] = str(v).strip()
        elif m := re.fullmatch(r"Base Bid Amount1\.(\d+)\.0", k):
            if str(v).strip():
                base_bids[int(m.group(1))] = parse_money(str(v))

    return {
        "airport": get("Airport Name"),
        "scope": get("Project Scope"),
        "csj": get("TxDOT CSJ"),
        "date": get("Bid Opening Date"),
        "time": get("Bid Opening Time"),
        "location": get("Bid Opening Location"),
        # list of (name, base bid) in bid-opening order
        "bidders": [(bidders[i], base_bids.get(i)) for i in sorted(bidders)],
    }


def _parse_bid_datetime(date_str: str, time_str: str) -> datetime | None:
    for fmt in ("%B %d, %Y %I:%M %p", "%m/%d/%Y %I:%M %p", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(f"{date_str} {time_str}".strip(), fmt)
        except ValueError:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------------
# Value cleanup
# --------------------------------------------------------------------------

def parse_money(text: str) -> float | None:
    """'$150,000.00' -> 150000.0. Every price on the form is printed with cents,
    so if OCR dropped the decimal point the last two digits are the cents."""
    digits = re.sub(r"[^\d.]", "", text or "")
    if not digits:
        return None
    if "." not in digits and len(digits) > 2:
        digits = f"{digits[:-2]}.{digits[-2:]}"
    try:
        return float(digits.rstrip(".")) if digits.count(".") <= 1 else None
    except ValueError:
        return None


def parse_qty(text: str) -> float | None:
    """'18,750' -> 18750.0"""
    digits = re.sub(r"[^\d.]", "", (text or "").replace(",", ""))
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def normalize_item(text: str) -> str:
    """Clean OCR'd item codes: 'F-164,2' -> 'F-164.2', 'f164.3C' -> 'F-164.3c'."""
    s = (text or "").replace(" ", "").replace(",", ".")
    m = re.fullmatch(r"([A-Za-z]{1,3})-?(\d+)(\.\d+)?([A-Za-z])?", s)
    if not m:
        return s.upper()
    prefix, num, dec, suffix = m.groups()
    return f"{prefix.upper()}-{num}{dec or ''}{(suffix or '').lower()}"


def clean_description(text: str) -> str:
    text = text.replace("°", "'").replace("’", "'").replace("|", " ")
    return re.sub(r"\s+", " ", text).strip().upper()


# --------------------------------------------------------------------------
# Scanned pages
# --------------------------------------------------------------------------

@dataclass
class RawRow:
    item: str
    qty: float | None
    unit: str
    description: str
    unit_price: float | None
    total: float | None


COLUMN_KEYS = [  # header text -> field; order matters ("unit price" before "unit")
    ("item", "item"), ("qty", "qty"), ("quan", "qty"), ("unit price", "unit_price"),
    ("unit", "unit"), ("desc", "description"), ("total", "total"),
]


def _map_columns(header_cells: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, text in enumerate(header_cells):
        t = text.lower()
        for needle, key in COLUMN_KEYS:
            if needle in t and key not in mapping:
                mapping[key] = idx
                break
    return mapping


def read_bid_table(gray, grid: ocr.TableGrid) -> list[RawRow]:
    """OCR a bid tab table cell by cell."""
    def box(r, c):
        (y0, y1), (x0, x1) = grid.rows[r], grid.cols[c]
        return (x0, y0, x1, y1)

    header = [ocr.ocr_cell(gray, box(0, c), ocr.TEXT) for c in range(len(grid.cols))]
    cols = _map_columns(header)
    missing = {"item", "qty", "unit", "description", "unit_price", "total"} - cols.keys()
    if missing:
        raise ValueError(f"bid tab header not recognized (missing {sorted(missing)}): {header}")

    rows = []
    for r in range(1, len(grid.rows)):
        item = normalize_item(ocr.ocr_cell(gray, box(r, cols["item"]), ocr.ITEM))
        if not item:
            continue  # blank row
        rows.append(RawRow(
            item=item,
            qty=parse_qty(ocr.ocr_cell(gray, box(r, cols["qty"]), ocr.NUMERIC)),
            unit=ocr.ocr_cell(gray, box(r, cols["unit"]), ocr.UNIT),
            description=clean_description(ocr.ocr_cell(gray, box(r, cols["description"]), ocr.TEXT)),
            unit_price=parse_money(ocr.ocr_cell(gray, box(r, cols["unit_price"]), ocr.NUMERIC)),
            total=parse_money(ocr.ocr_cell(gray, box(r, cols["total"]), ocr.NUMERIC)),
        ))
    return rows


def read_bid_form(text: str) -> tuple[str, str]:
    """Pull (bidder name, project description) from OCR text of a bid form page."""
    name = ""
    if m := re.search(r"Name of Bidder[:\s]*(.+)", text):
        name = m.group(1).strip()
    desc = ""
    if m := re.search(r"Project Description:?\s*\n\s*(.+)", text):
        desc = m.group(1).strip()
    return name, desc


# --------------------------------------------------------------------------
# Cross-checks
# --------------------------------------------------------------------------

def reconcile(rows: list[RawRow], base_bid: float | None) -> tuple[dict[str, float], dict[str, float], bool, list[str]]:
    """Resolve OCR disagreements in one bidder's table.

    For each line, qty x unit price should equal the total. When it doesn't,
    one of the two numbers was misread; we try both readings and keep the
    combination whose line totals add up to the typed base bid.

    Returns (unit_prices, totals, verified, notes).
    """
    notes: list[str] = []
    options: list[list[tuple[float, float]]] = []  # per row: candidate (unit_price, total)
    for r in rows:
        q, up, tot = r.qty, r.unit_price, r.total
        if q is None or (up is None and tot is None):
            options.append([(up or 0.0, tot or 0.0)])
            notes.append(f"{r.item}: could not read qty/price (qty={q}, unit={up}, total={tot})")
            continue
        if up is None:
            options.append([(round(tot / q, 2), tot)])
        elif tot is None:
            options.append([(up, round(up * q, 2))])
        elif abs(up * q - tot) < CENT:
            options.append([(up, tot)])
        else:
            options.append([(up, round(up * q, 2)), (round(tot / q, 2), tot)])

    ambiguous = [i for i, o in enumerate(options) if len(o) > 1]
    chosen = [o[0] for o in options]  # default: trust the unit price
    verified = False
    if base_bid is not None and len(ambiguous) <= 10:
        for picks in itertools.product(*(options[i] for i in ambiguous)):
            trial = list(chosen)
            for i, pick in zip(ambiguous, picks):
                trial[i] = pick
            if abs(sum(t for _, t in trial) - base_bid) < CENT:
                chosen, verified = trial, True
                break

    for i in ambiguous:
        r = rows[i]
        up, tot = chosen[i]
        fixed = "total" if up == r.unit_price else "unit price"
        notes.append(f"{r.item}: OCR read unit price {r.unit_price:,.2f} / total {r.total:,.2f}, "
                     f"which disagree with qty {r.qty:g}; corrected {fixed} -> "
                     f"{up:,.2f} / {tot:,.2f}")
    if base_bid is not None and not verified:
        notes.append(f"line totals sum to {sum(t for _, t in chosen):,.2f} but base bid is "
                     f"{base_bid:,.2f}; NOT verified, check this bidder manually")

    unit_prices = {r.item: c[0] for r, c in zip(rows, chosen)}
    totals = {r.item: c[1] for r, c in zip(rows, chosen)}
    return unit_prices, totals, verified, notes


def _consensus_rows(tables: list[list[RawRow]], notes: list[str]) -> list[BidItem]:
    """Every bidder prices the same items. Use a majority vote across bidders
    for item code, qty, unit and description so one bad OCR read can't win."""
    n = Counter(len(t) for t in tables).most_common(1)[0][0]
    items = []
    for i in range(n):
        cells = [t[i] for t in tables if len(t) == n]

        def vote(attr):
            vals = [getattr(c, attr) for c in cells if getattr(c, attr) not in (None, "")]
            return Counter(vals).most_common(1)[0][0] if vals else None

        item = BidItem(item=vote("item"), qty=vote("qty"), unit=vote("unit"),
                       description=vote("description") or "")
        items.append(item)
        for c in cells:
            if c.item != item.item or c.qty != item.qty:
                notes.append(f"row {i + 1}: one bidder read as {c.item} qty {c.qty}; "
                             f"using consensus {item.item} qty {item.qty}")
                c.item, c.qty = item.item, item.qty
    return items


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def extract_project(pdf_path: Path, listing: ProjectListing | None = None) -> Project:
    """Read the PDF and return the structured, cross-checked project data."""
    form = read_summary_form(pdf_path)
    if not form.get("bidders"):
        raise ValueError(f"{pdf_path.name}: no bid summary form found; is this an "
                         "old-format (pre Oct 2026) PDF?")
    notes: list[str] = []

    # Pages after the summary alternate: bid form (bidder name) then bid tab.
    pages = ocr.render_pages(pdf_path)
    current_name, description = None, ""
    tables: list[tuple[str, list[RawRow]]] = []
    for page_no, gray in enumerate(pages[1:], start=2):
        grid = ocr.find_table(gray)
        if grid and len(grid.rows) > 2:
            if current_name is None:
                notes.append(f"page {page_no}: bid tab without a preceding bid form page")
                current_name = f"Unknown bidder {len(tables) + 1}"
            tables.append((current_name, read_bid_table(gray, grid)))
            log.info("page %d: bid tab for %s (%d rows)", page_no, current_name, len(tables[-1][1]))
            current_name = None
        else:
            name, desc = read_bid_form(ocr.page_text(gray))
            if name:
                current_name = name
                description = description or desc
                log.info("page %d: bid form for %s", page_no, name)

    # Bidder names / base bids from the typed form are authoritative; match the
    # OCR'd names to them.
    form_names = [n for n, _ in form["bidders"]]
    base_by_name = dict(form["bidders"])
    items = _consensus_rows([rows for _, rows in tables], notes)

    bids = []
    for ocr_name, rows in tables:
        match = difflib.get_close_matches(ocr_name, form_names, n=1, cutoff=0.6)
        name = match[0] if match else ocr_name
        if not match:
            notes.append(f"bidder '{ocr_name}' not found on the summary sheet")
        base = base_by_name.get(name)
        unit_prices, totals, verified, bid_notes = reconcile(rows, base)
        notes += [f"{name}: {n}" for n in bid_notes]
        bids.append(Bid(bidder=name, base_bid=base if base is not None else sum(totals.values()),
                        unit_prices=unit_prices, totals=totals, verified=verified))

    missing = set(form_names) - {b.bidder for b in bids}
    for name in missing:
        notes.append(f"{name}: listed on summary sheet but no bid tab found in PDF")

    bid_date = (_parse_bid_datetime(form["date"], form["time"])
                or (listing.opening if listing else None))
    if bid_date is None:
        raise ValueError("could not determine bid opening date")

    return Project(
        project_id=listing.project_id if listing else _project_id_from_name(pdf_path, form["csj"]),
        airport=form["airport"] or (listing.title if listing else ""),
        csj=form["csj"],
        scope=form["scope"],
        description=description,
        bid_date=bid_date,
        location=form["location"],
        items=items,
        bids=bids,
        notes=notes,
    )


def _project_id_from_name(pdf_path: Path, csj: str) -> str:
    """Offline runs have no listing; TxDOT names files after the project ID."""
    m = re.search(r"(ntb\w+?\d{5})", pdf_path.stem, re.IGNORECASE)
    return m.group(1).upper() if m else csj


def validate(project: Project) -> list[str]:
    """Return problems that should block or be reviewed (empty = data checks out)."""
    problems = [f"{b.bidder}: line totals do not reconcile to base bid"
                for b in project.bids if not b.verified]
    for b in project.bids:
        missing = [i.item for i in project.items if i.item not in b.unit_prices]
        if missing:
            problems.append(f"{b.bidder}: no price for {', '.join(missing)}")
    return problems
