"""Load the legacy bid tabs (data/legacy/*.json) into the master unit-price workbook.

Each project goes through master_sheet.upsert_project(), the same path the new-format
pipeline uses, so re-running replaces a project's rows instead of duplicating them.
A "Legacy Tabs Log" sheet is then added with each project's verification status and
extraction notes (it is rebuilt every time this script runs).

usage: python scripts/build_master_legacy.py [--out output/master_unit_prices.xlsx]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from txdot_bid_tabs import legacy, master_sheet  # noqa: E402
from txdot_bid_tabs.config import MASTER_SHEET_PATH  # noqa: E402

EXCLUDED = {
    "2520LBRTY_CompleteBids.pdf": "One contractor's full bid proposal (Apex Alliance), not a tabulation of "
                                  "competing bids; left out so it doesn't skew the low-bid prices.",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=MASTER_SHEET_PATH)
    ap.add_argument("--include-unverified", action="store_true",
                    help="also load projects whose line items don't reconcile to the printed totals")
    a = ap.parse_args()

    log = []
    for f in sorted(legacy.LEGACY_DIR.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        project = legacy.load(f)
        if not project.bids:
            log.append((d["project_id"], d["airport"], "SKIPPED: no bidder line items", ""))
            continue
        n_ok = sum(b.verified for b in project.bids)
        if n_ok < len(project.bids) and not a.include_unverified:
            log.append((project.project_id, project.airport,
                        f"PENDING: {n_ok}/{len(project.bids)} bidders reconcile; not loaded", ""))
            print(f"{project.project_id:<12} pending ({n_ok}/{len(project.bids)} bidders reconcile) - not loaded")
            continue
        master_sheet.upsert_project(project, path=a.out, source_url=f"samples/legacy/{d['source_pdf']}")
        status = "Verified" if n_ok == len(project.bids) else f"{n_ok}/{len(project.bids)} bidders verified"
        log.append((project.project_id, project.airport, status, " | ".join(project.notes)))
        print(f"{project.project_id:<12} {status:<26} {len(project.items):>4} items  low: {project.low_bid.bidder}")

    done = {json.loads(f.read_text(encoding="utf-8"))["source_pdf"] for f in legacy.LEGACY_DIR.glob("*.json")}
    for pdf in sorted((ROOT / "samples" / "legacy").glob("*.pdf")):
        if pdf.name not in done:
            if pdf.name in EXCLUDED:
                log.append((pdf.stem[:12], pdf.name, "EXCLUDED", EXCLUDED[pdf.name]))
            else:
                log.append((pdf.stem[:12], pdf.name, "PENDING: not extracted yet", ""))

    wb = load_workbook(a.out)
    if "Legacy Tabs Log" in wb.sheetnames:
        del wb["Legacy Tabs Log"]
    ws = wb.create_sheet("Legacy Tabs Log")
    head = ["Project ID", "Airport", "Status", "Notes"]
    for j, h in enumerate(head, start=1):
        c = ws.cell(row=1, column=j, value=h)
        c.font = Font(name="Arial", bold=True)
        c.fill = PatternFill("solid", fgColor="D9E1F2")
    log.sort(key=lambda r: (not r[2].startswith("Verified"), r[0]))
    for i, row in enumerate(log, start=2):
        for j, v in enumerate(row, start=1):
            c = ws.cell(row=i, column=j, value=v)
            c.font = Font(name="Arial", size=10)
            c.alignment = Alignment(vertical="top", wrap_text=(j == 4))
            if j == 3 and not str(v).startswith("Verified"):
                c.fill = PatternFill("solid", fgColor="FCE4D6")
    for col, w in zip("ABCD", (14, 44, 26, 120)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    wb.save(a.out)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
