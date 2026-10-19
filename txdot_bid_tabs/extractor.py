"""Stage 3: parse a bid-tab PDF into a Project.

PDF layout (new format, Oct 2026+), confirmed on the Lockhart PDF:
  page 1     Bid Result Summary Sheet. A *fillable form*: airport, scope, CSJ,
             date/time/location, bidder names and base bids are form fields, so
             they are read exactly with no OCR.
  then, per bidder, in bid-opening order (NOT price order), a scanned bid form.
  Two variants have been seen:
    Lockhart (4-page form)  page 1: Name of Bidder (OCR); page 2: bid tab table.
                            The Item column holds the spec code ("F-164.2").
    Zapata (6-page form)    page 1: Name of Bidder above the start of the bid tab;
                            the table continues over the next pages, then a
                            "Bid Summary" page. The Item column is a line number
                            and the spec code leads the description
                            ("Item P-152-4.1, Drainage Excavation ...").
  A page naming a bidder starts that bidder; every table page after it, up to
  the next name, adds rows to the same bidder.

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


SPEC_CODE = re.compile(r"[A-Z]{1,3}-\d")
# Spec code leading a (cleaned, upper-case) description, as in the Zapata form:
#   "C-105, CONTRACTOR MOBILIZATION"     "ITEM PARKHILL P-140-3.1, TEMPORARY ..."
#   "ITEM MC1-1, FURNISH AND INSTALL"    "TXDOT ITEM 341 HOT MIX ASPHALT, ..."
# OCR sometimes drops the "I" of "ITEM" or adds a stray mark before it.
_DESC_CODE = re.compile(
    r"^\W*(?:(?P<txdot>TXDOT)\s+)?(?:I?TEM\s+)?(?:[A-Z]+\s+)??"
    r"(?P<code>[A-Z]{1,3}\d*-[0-9A-Z][0-9A-Z.\-]*|(?(txdot)\d+(?:-[\d.]+)?|(?!)))"
    r"(?:\s*[,.;:]\s*|\s+)(?P<rest>.+)$")


def split_description_code(description: str) -> tuple[str, str] | None:
    """'ITEM P-152-4.1, DRAINAGE EXCAVATION' -> ('P-152-4.1', 'DRAINAGE EXCAVATION').
    TxDOT standard items get a TX- prefix to match the Lockhart codes ('TX-341').
    Returns None if the description doesn't start with a spec code."""
    m = _DESC_CODE.match(description)
    if not m:
        return None
    code = m.group("code").rstrip(".-")
    if m.group("txdot"):
        code = f"TX-{code}"
    code = re.sub(r"(\d)([A-Z])$", lambda g: g[1] + g[2].lower(), code)  # 5.1A -> 5.1a
    return code, m.group("rest").strip()


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
    # One unreadable header cell (e.g. "Qty" OCR'd as "aty") is unambiguous:
    # it's the one column left over.
    missing = {k for _, k in COLUMN_KEYS} - mapping.keys()
    unmapped = set(range(len(header_cells))) - set(mapping.values())
    if len(missing) == 1 and len(unmapped) == 1:
        mapping[missing.pop()] = unmapped.pop()
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
        description = clean_description(ocr.ocr_cell(gray, box(r, cols["description"]), ocr.TEXT))
        if not item and not description:
            continue  # blank row
        # Item column holding a line number: the spec code leads the description.
        if not SPEC_CODE.match(item) and (split := split_description_code(description)):
            item, description = split
        rows.append(RawRow(
            item=item,
            qty=parse_qty(ocr.ocr_cell(gray, box(r, cols["qty"]), ocr.NUMERIC)),
            unit=ocr.ocr_cell(gray, box(r, cols["unit"]), ocr.UNIT),
            description=description,
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
        desc = m.group(1).strip(" -")
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
            # the total may itself be off by a stray digit ($11,810.06 for
            # $11,810.00); the unit price it implies, rounded to cents, fixes that
            derived = round(tot / q, 2)
            options.append(list(dict.fromkeys([(derived, tot), (derived, round(derived * q, 2))])))
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

    # Both numbers on a line misread ($500.50 / $5,506.10 for $500.56 / $5,506.16):
    # no reading reconciles. If exactly one line can absorb the shortfall, i.e. the
    # base bid implies a total that is a whole-cent unit price x qty and within 1%
    # of what OCR read, solve that line from the base bid.
    solved = None
    if base_bid is not None and not verified:
        fixes = []
        for i in ambiguous:
            q = rows[i].qty
            total = round(base_bid - sum(t for k, (_, t) in enumerate(chosen) if k != i), 2)
            up = round(total / q, 2)
            if (total > 0 and abs(up * q - total) < CENT
                    and any(abs(total - t) <= 0.01 * t for _, t in options[i])):
                fixes.append((i, (up, total)))
        if len(fixes) == 1:
            solved, pick = fixes[0]
            chosen[solved], verified = pick, True

    for i in ambiguous:
        r = rows[i]
        up, tot = chosen[i]
        if i == solved:
            notes.append(f"{r.item}: OCR read unit price {r.unit_price or 0:,.2f} / total "
                         f"{r.total or 0:,.2f} (qty {r.qty:g}), neither consistent; solved from "
                         f"the base bid -> {up:,.2f} / {tot:,.2f}")
            continue
        if r.unit_price is None:
            if tot != r.total:
                notes.append(f"{r.item}: unit price unreadable and total {r.total:,.2f} disagrees "
                             f"with qty {r.qty:g}; corrected total -> {up:,.2f} / {tot:,.2f}")
            continue
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


UNITS = {"LS", "LF", "EA", "SY", "SF", "CY", "TON", "GAL", "AC", "HR", "DAY", "MO", "LB", "SM", "CF"}


def _consensus_rows(tables: list[list[RawRow]], notes: list[str]) -> list[BidItem]:
    """Every bidder prices the same items. Use a majority vote across bidders
    for item code, qty, unit and description so one bad OCR read can't win."""
    n = Counter(len(t) for t in tables).most_common(1)[0][0]
    items = []
    for i in range(n):
        cells = [t[i] for t in tables if len(t) == n]

        def vote(attr):
            vals = [getattr(c, attr) for c in cells if getattr(c, attr) not in (None, "")]
            if attr == "unit":  # a clipped read ("A" for "EA") shouldn't outvote a real unit
                vals = [v for v in vals if v in UNITS] or vals
            counts = Counter(vals)
            if attr == "qty":
                # Prefer the qty under which the most bidders' unit price x qty
                # equals their line total; plain votes only break ties ("11" read
                # as "1" by one bidder and missed by another is otherwise a coin toss).
                def fits(q):
                    return sum(c.unit_price is not None and c.total is not None
                               and abs(c.unit_price * q - c.total) < CENT for c in cells)
                return max(counts, key=lambda q: (fits(q), counts[q])) if counts else None
            return counts.most_common(1)[0][0] if vals else None

        item = BidItem(item=vote("item"), qty=vote("qty"), unit=vote("unit"),
                       description=vote("description") or "")
        if item.item in {it.item for it in items}:  # codes key the prices, so keep them unique
            notes.append(f"row {i + 1}: item code {item.item} repeats an earlier row; "
                         f"recorded as {item.item}#{i + 1}")
            item.item = f"{item.item}#{i + 1}"
        items.append(item)
        for c in cells:
            if c.item != item.item or c.qty != item.qty:
                notes.append(f"row {i + 1}: one bidder read as {c.item} qty {c.qty}; "
                             f"using consensus {item.item} qty {item.qty}")
                c.item, c.qty = item.item, item.qty
    return items


_NAME_WORDS = {"co": "company", "corp": "corporation", "incorporated": "inc", "ltd": "limited"}


def _name_key(name: str) -> str:
    """'ANDERSON COLUMBIA CO., INC.' -> 'anderson columbia company inc'"""
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    return " ".join(_NAME_WORDS.get(w, w) for w in words)


def match_bidders(ocr_names: list[str], form_names: list[str]) -> dict[str, str | None]:
    """Map each OCR'd bidder name to its typed summary-sheet name (None if no match).

    Names are compared case-insensitively with common abbreviations expanded
    (bid forms say "CO., INC." where the summary sheet says "Company, Inc.").
    If exactly one name on each side is left over, they're paired by elimination."""
    keys = {_name_key(f): f for f in form_names}
    result: dict[str, str | None] = {}
    for o in ocr_names:
        m = difflib.get_close_matches(_name_key(o), list(keys), n=1, cutoff=0.6)
        result[o] = keys[m[0]] if m else None
    left_ocr = [o for o, f in result.items() if f is None]
    left_form = [f for f in form_names if f not in result.values()]
    if len(left_ocr) == 1 and len(left_form) == 1:
        result[left_ocr[0]] = left_form[0]
    return result


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

    # Pages after the summary: each bidder's bid form names the bidder, then
    # the bid tab follows on the same page and/or the next ones.
    pages = ocr.render_pages(pdf_path)
    description = ""
    tables: list[tuple[str, list[RawRow]]] = []  # (OCR'd bidder name, rows) per bidder
    for page_no, gray in enumerate(pages[1:], start=2):
        grid = ocr.find_table(gray)
        is_tab = bool(grid and len(grid.rows) > 2)
        # The bidder's name sits above the table on a combined page; skip OCR
        # when there's only a page header above it (continuation pages).
        top = grid.rows[0][0] if is_tab else gray.shape[0]
        name, desc = read_bid_form(ocr.page_text(gray[:top])) if top > ocr.RENDER_DPI else ("", "")
        if name:
            tables.append((name, []))
            description = description or desc
            log.info("page %d: bid form for %s", page_no, name)
        if is_tab:
            if not tables:
                notes.append(f"page {page_no}: bid tab without a preceding bid form page")
                tables.append((f"Unknown bidder {len(tables) + 1}", []))
            rows = read_bid_table(gray, grid)
            tables[-1][1].extend(rows)
            log.info("page %d: bid tab for %s (%d rows)", page_no, tables[-1][0], len(rows))
    for name, rows in tables:
        if not rows:
            notes.append(f"{name}: bid form found but no bid tab rows")
    tables = [t for t in tables if t[1]]
    if not tables:
        raise ValueError(f"{pdf_path.name}: no bid tab tables found")

    # Bidder names / base bids from the typed form are authoritative; match the
    # OCR'd names to them.
    form_names = [n for n, _ in form["bidders"]]
    base_by_name = dict(form["bidders"])
    items = _consensus_rows([rows for _, rows in tables], notes)

    matches = match_bidders([n for n, _ in tables], form_names)
    bids = []
    for ocr_name, rows in tables:
        name = matches[ocr_name] or ocr_name
        if matches[ocr_name] is None:
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
