"""Legacy (pre-October 2026) bid tabs -> Project objects for the master sheet.

The 27 historical tabs come from many engineering firms in many layouts, so
they are not parsed by extractor.py. Instead each tab is extracted once
(scripts/legacy_words.py + scripts/legacy_rows.py, then checked by a person)
into a JSON file in data/legacy/, one per project:

{
  "project_id": "2412CONRO",            # TxDOT project / CSJ code
  "airport": "Conroe-North Houston Regional Airport",
  "csj": "2412CONRO", "scope": "Pavement Markings", "description": "...",
  "bid_date": "2025-10-30", "location": "",
  "source_pdf": "2412CONRO_Bid Tab.pdf",
  "columns": ["Engineer's Estimate", "Lone Wolf Construction, LLC", ...],
  "estimate_column": "Engineer's Estimate",          # or null
  "award_schedules": ["Schedule 2"],                 # schedules that make up the
                                                     # bid used to rank bidders (null = all)
  "stated_totals": {"Lone Wolf Construction, LLC": 976400.00, ...},   # as printed
  "items": [
    {"schedule": "Schedule 2", "item": "C-105-6.1", "description": "Mobilization",
     "unit": "LS", "qty": 1, "prices": [107000.00, 85000.00, 85000.00]},   # unit price per column, null = no bid
  ],
  "notes": ["..."]
}

A bidder's ranking bid is the sum of qty x unit price over the award schedules.
It is checked against "stated_totals"; any difference over $1 marks the bidder
unverified and is reported, never silently corrected.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .config import ROOT_DIR
from .models import Bid, BidItem, Project

LEGACY_DIR = ROOT_DIR / "data" / "legacy"
TOLERANCE = 1.00


def _item_code(code: str, project_id: str) -> str:
    """Tabs that number items 1, 2, 3 (no FAA/TxDOT spec code) get the project id as a
    prefix, so the master view doesn't merge unrelated items from different projects."""
    code = (code or "").strip()
    if not code or re.fullmatch(r"[A-Z]?\d{1,3}(\.\d{1,3})?[A-Za-z]?\.?", code):
        return f"{project_id}#{code.rstrip('.') or '?'}"
    return code


def load(path: Path) -> Project:
    d = json.loads(path.read_text(encoding="utf-8"))
    items = [BidItem(item=_item_code(i["item"], d["project_id"]), description=i["description"].lstrip("=+@"),
                     unit=i["unit"], qty=float(i["qty"]), schedule=i.get("schedule", ""), line=n)
             for n, i in enumerate(d["items"], start=1)]
    award = set(d.get("award_schedules") or [])
    notes = list(d.get("notes", []))
    bids: list[Bid] = []
    estimate = None
    for c, name in enumerate(d["columns"]):
        up, tot = {}, {}
        for it, raw in zip(items, d["items"]):
            price = raw["prices"][c]
            if price is None:
                continue
            up[it.key] = float(price)
            tot[it.key] = round(it.qty * float(price), 2)
        ranked_sum = round(sum(v for it, v in ((it, tot.get(it.key)) for it in items)
                               if v is not None and (not award or it.schedule in award)), 2)
        stated = (d.get("stated_totals") or {}).get(name)
        verified = stated is not None and abs(ranked_sum - stated) <= TOLERANCE
        if stated is None:
            notes.append(f"{name}: no printed total to check against (computed ${ranked_sum:,.2f})")
        elif not verified:
            notes.append(f"{name}: computed ${ranked_sum:,.2f} vs printed ${stated:,.2f} "
                         f"(diff ${ranked_sum - stated:,.2f})")
        bid = Bid(bidder=name, base_bid=stated if stated is not None else ranked_sum,
                  unit_prices=up, totals=tot, verified=verified)
        if name == d.get("estimate_column"):
            estimate = bid
        elif up:
            bids.append(bid)
    return Project(project_id=d["project_id"], airport=d["airport"], csj=d.get("csj", ""),
                   scope=d.get("scope", ""), description=d.get("description", ""),
                   bid_date=datetime.fromisoformat(d["bid_date"]), location=d.get("location", ""),
                   items=items, bids=bids, notes=notes, estimate=estimate, source="legacy tab")


def load_all(folder: Path = LEGACY_DIR) -> list[tuple[Project, str]]:
    out = []
    for f in sorted(folder.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        out.append((load(f), d.get("source_pdf", "")))
    return out
