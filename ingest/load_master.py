"""Load output/master_unit_prices.xlsx into the Estimate Master Database (SPEC.md "Loader rules").

Source: the workbook's Projects and Bid Data sheets, enriched with
data/legacy/<project_id>.json (award schedules, printed totals, notes),
data/project_airports.csv, data/unit_corrections.csv and data/bidder_merges.csv.

- One transaction per project; idempotent. Upserts skip unchanged rows, so a
  re-run reports 0 inserted / 0 updated.
- Pay-item matching is not done here: pay_item_id / match_* are never written,
  and needs_review is only ever switched on (never cleared) by a re-run.
- Nothing is deleted. Rows in the database that are no longer in the workbook
  are reported.

Run:
  python ingest/load_master.py --bidders           # bidder normalization table only
  python ingest/load_master.py [--dry-run]         # load all projects
  python ingest/load_master.py --project 2509TEMPL # load one project
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
WORKBOOK = ROOT / "output" / "master_unit_prices.xlsx"
LEGACY_DIR = ROOT / "data" / "legacy"
PROJECT_AIRPORTS_CSV = ROOT / "data" / "project_airports.csv"
UNIT_CORRECTIONS_CSV = ROOT / "data" / "unit_corrections.csv"
BIDDER_MERGES_CSV = ROOT / "data" / "bidder_merges.csv"

# Legal-form words dropped anywhere in a bidder name (SPEC Loader rule 3).
LEGAL_FORMS = {"LLC", "INC", "CO", "LTD", "CORP", "COMPANY",
               "CORPORATION", "INCORPORATED", "LP", "LLP"}

ESTIMATE_BIDDER = "ENGINEERS ESTIMATE"
ESTIMATE_DISPLAY = "Engineer's Estimate"
NEW_FORM_SCHEDULE = "Base Bid"   # new-format tabs have no schedules


# ---------------------------------------------------------------- bidders

def normalize_bidder(name):
    """Upper case, strip punctuation, drop legal-form words.

    'Don Jackson Construction, Inc.' -> 'DON JACKSON CONSTRUCTION'
    'EAS Contracting, L.P.'          -> 'EAS CONTRACTING'
    'F&W Electrical'                 -> 'F W ELECTRICAL'
    """
    s = name.upper()
    s = re.sub(r"[.'’]", "", s)      # H.V. -> HV, L.P. -> LP, ENGINEER'S -> ENGINEERS
    s = re.sub(r"[^A-Z0-9]+", " ", s)     # & - , ( ) / -> space
    return " ".join(w for w in s.split() if w not in LEGAL_FORMS)


def read_merges(path=BIDDER_MERGES_CSV):
    """name_normalized -> canonical name_normalized (hand-confirmed near-duplicates)."""
    return {r["from_normalized"].strip(): r["to_normalized"].strip() for r in read_csv(path)} if path.exists() else {}


def bidder_key(raw_name, is_estimate, merges):
    if is_estimate:
        return ESTIMATE_BIDDER
    key = normalize_bidder(raw_name)
    return merges.get(key, key)


def bidder_table(bid_rows, merges):
    """key -> {raw name -> set of project ids}."""
    table = defaultdict(lambda: defaultdict(set))
    for r in bid_rows:
        key = bidder_key(r["Bidder"], r["Low Bid"] == "Estimate", merges)
        table[key][r["Bidder"]].add(r["Project ID"])
    return table


def display_name(key, raw_names):
    """Prefer a spelling that normalizes to the key itself, then most projects, then longest."""
    if key == ESTIMATE_BIDDER:
        return ESTIMATE_DISPLAY
    return max(raw_names, key=lambda n: (normalize_bidder(n) == key, len(raw_names[n]), len(n)))


def print_bidder_table(table):
    print(f"{'name_normalized':<45} {'name (display)':<45} raw spellings (projects)")
    print("-" * 140)
    for key in sorted(table):
        raws = table[key]
        spellings = "; ".join(f"{n} ({len(p)})" for n, p in sorted(raws.items()))
        print(f"{key:<45} {display_name(key, raws):<45} {spellings}")
    print(f"\n{len(table)} bidders from {sum(len(r) for r in table.values())} raw spellings")


# ---------------------------------------------------------------- inputs

def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k: (v or "").strip() or None for k, v in row.items()} for row in csv.DictReader(f)]


def read_sheet(wb, name):
    rows = wb[name].iter_rows(values_only=True)
    header = next(rows)
    return [dict(zip(header, r)) for r in rows if any(v is not None for v in r)]


def dec(x):
    """Exact Decimal for a workbook number (13.22 -> Decimal('13.22'), not the binary float)."""
    if x is None:
        return None
    return Decimal(repr(x)) if isinstance(x, float) else Decimal(x)


def as_date(x):
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def build_projects(wb):
    """All projects as plain dicts, ready to load. Pure: no database access."""
    project_rows = {p["Project ID"]: p for p in read_sheet(wb, "Projects")}
    bid_rows = read_sheet(wb, "Bid Data")
    merges = read_merges()
    corrections = {(r["project_id"], r["item"]): r["unit"].upper() for r in read_csv(UNIT_CORRECTIONS_CSV)}
    airports = defaultdict(list)
    for r in read_csv(PROJECT_AIRPORTS_CSV):
        airports[r["project_id"]].append(r["faa_id"].upper())

    table = bidder_table(bid_rows, merges)
    bidder_names = {key: display_name(key, raws) for key, raws in table.items()}

    rows_by_project = defaultdict(list)
    for r in bid_rows:
        rows_by_project[r["Project ID"]].append(r)
    unknown = set(rows_by_project) - set(project_rows)
    if unknown:
        raise SystemExit(f"Bid Data has projects missing from the Projects sheet: {sorted(unknown)}")

    projects = []
    for pid, p in project_rows.items():
        legacy = p["Source"] == "legacy tab"
        js = None
        if legacy:
            path = LEGACY_DIR / f"{pid}.json"
            if not path.exists():
                raise SystemExit(f"{pid}: legacy project without {path.relative_to(ROOT)}")
            js = json.loads(path.read_text(encoding="utf-8"))
        if not airports.get(pid):
            raise SystemExit(f"{pid}: not in {PROJECT_AIRPORTS_CSV.name} (the loader never creates airports)")

        award_schedules = js["award_schedules"] if legacy else [NEW_FORM_SCHEDULE]
        stated = js["stated_totals"] if legacy else {}

        bids = {}
        for r in rows_by_project[pid]:
            is_est = r["Low Bid"] == "Estimate"
            key = bidder_key(r["Bidder"], is_est, merges)
            bid = bids.setdefault(key, {
                "key": key, "raw": r["Bidder"], "is_estimate": is_est,
                "is_low_in_workbook": False, "workbook_rank": r["Bid Rank"],
                "verified": True, "lines": [],
            })
            if bid["raw"] != r["Bidder"]:
                raise SystemExit(f"{pid}: {r['Bidder']!r} and {bid['raw']!r} both normalize to {key!r}")
            bid["is_low_in_workbook"] |= r["Low Bid"] == "Yes"
            bid["verified"] &= r["Verified"] == "Yes"

            raw_unit = r["Unit"]
            unit = corrections.get((pid, r["Item"])) or (raw_unit or "").strip().upper() or None
            bid["lines"].append({
                "line_no": len(bid["lines"]) + 1,
                "schedule": r["Schedule"] or NEW_FORM_SCHEDULE,
                "raw_item_code": r["Item"], "raw_description": r["Description"], "raw_unit": raw_unit,
                "unit": unit, "quantity": dec(r["Qty"]), "unit_price": dec(r["Unit Price"]),
                "total": dec(r["Total"]),
            })

        for bid in bids.values():
            if legacy:
                total = stated.get(bid["raw"])
                bid["award_total"] = dec(total)
                # The JSON holds one printed total per bidder over the award schedules; it is a
                # per-schedule total only when there is a single award schedule.
                bid["schedule_totals"] = (
                    {award_schedules[0]: dec(total)} if total is not None and len(award_schedules) == 1 else {}
                )
            else:
                bid["award_total"] = (dec(p["Low Bid"]) if bid["is_low_in_workbook"]
                                      else sum(l["total"] for l in bid["lines"]))
                bid["schedule_totals"] = {}

        notes = js.get("notes") if legacy else None
        if isinstance(notes, list):
            notes = "\n".join(notes)
        projects.append({
            "project_id": pid,
            "csj": p["CSJ"],
            "airport_name_raw": p["Airport"],
            "scope": p["Scope"],
            "description": p["Description"],
            "bid_date": as_date(p["Bid Date"]),
            "location": p["Location"],
            "source": p["Source"],
            "source_pdf": p["Source PDF"],
            "award_schedules": award_schedules,
            "notes": notes or None,
            "airports": airports[pid],
            "bids": list(bids.values()),
        })
    return projects, bidder_names, table


# ---------------------------------------------------------------- database

def upsert(conn, table, key, cols, rows, set_exprs=None):
    """INSERT ... ON CONFLICT DO UPDATE that skips unchanged rows. Returns Counter(inserted, updated)."""
    set_exprs = set_exprs or {}
    upd = [c for c in cols if c not in key]
    new = [set_exprs.get(c, f"excluded.{c}") for c in upd]
    sql = (
        f"insert into public.{table} as t ({', '.join(cols)}) "
        f"values ({', '.join(f'%({c})s' for c in cols)}) "
        f"on conflict ({', '.join(key)}) do "
        + (f"update set {', '.join(f'{c} = {e}' for c, e in zip(upd, new))} "
           f"where ({', '.join(f't.{c}' for c in upd)}) is distinct from ({', '.join(new)}) "
           if upd else "nothing ")
        + "returning (xmax = 0) as inserted"
    )
    counts = Counter(inserted=0, updated=0)
    if not rows:
        return counts
    with conn.cursor() as cur:
        cur.executemany(sql, rows, returning=True)
        while True:
            for (inserted,) in cur.fetchall():
                counts["inserted" if inserted else "updated"] += 1
            if not cur.nextset():
                break
    return counts


def load_project(conn, proj, bidder_names, units):
    """Load one project inside the caller's transaction. Returns Counter of changes per table."""
    pid = proj["project_id"]
    changes = Counter()

    def add(table, c):
        changes[f"{table}.inserted"] += c["inserted"]
        changes[f"{table}.updated"] += c["updated"]

    missing = [f for f in proj["airports"]
               if conn.execute("select 1 from public.airports where faa_id = %s", (f,)).fetchone() is None]
    if missing:
        raise RuntimeError(f"{pid}: airports {missing} not in airports (run scripts/sync_airports.py)")

    pcols = ["project_id", "csj", "airport_name_raw", "scope", "description", "bid_date",
             "location", "source", "source_pdf", "award_schedules", "notes"]
    add("projects", upsert(conn, "projects", ["project_id"], pcols, [{c: proj[c] for c in pcols}]))
    project_pk = conn.execute("select id from public.projects where project_id = %s", (pid,)).fetchone()[0]

    add("project_airports", upsert(conn, "project_airports", ["project_id", "faa_id"], ["project_id", "faa_id"],
                                   [{"project_id": project_pk, "faa_id": f} for f in proj["airports"]]))

    keys = [b["key"] for b in proj["bids"]]
    existing = {r[0] for r in conn.execute(
        "select name_normalized from public.bidders where name_normalized = any(%s)", (keys,))}
    new_bidders = [k for k in keys if k not in existing]
    c = upsert(conn, "bidders", ["name_normalized"], ["name_normalized", "name"],
               [{"name_normalized": k, "name": bidder_names[k]} for k in keys])
    add("bidders", c)
    bidder_ids = dict(conn.execute(
        "select name_normalized, id from public.bidders where name_normalized = any(%s)", (keys,)).fetchall())

    bcols = ["project_id", "bidder_id", "is_estimate", "bidder_name_raw", "award_total", "verified"]
    add("bids", upsert(conn, "bids", ["project_id", "bidder_id"], bcols, [
        {"project_id": project_pk, "bidder_id": bidder_ids[b["key"]], "is_estimate": b["is_estimate"],
         "bidder_name_raw": b["raw"], "award_total": b["award_total"], "verified": b["verified"]}
        for b in proj["bids"]]))
    bid_ids = dict(conn.execute(
        "select bidder_id, id from public.bids where project_id = %s", (project_pk,)).fetchall())

    totals, lines = [], []
    for b in proj["bids"]:
        bid_id = bid_ids[bidder_ids[b["key"]]]
        totals += [{"bid_id": bid_id, "schedule": s, "printed_total": t} for s, t in b["schedule_totals"].items()]
        for l in b["lines"]:
            unit_ok = l["unit"] in units
            lines.append({
                "bid_id": bid_id, "line_no": l["line_no"], "schedule": l["schedule"],
                "raw_item_code": l["raw_item_code"], "raw_description": l["raw_description"],
                "raw_unit": l["raw_unit"], "quantity": l["quantity"], "unit_price": l["unit_price"],
                "unit_code": l["unit"] if unit_ok else None,
                "needs_review": not unit_ok,
                "review_notes": None if unit_ok else (f"unknown unit {l['unit']!r}" if l["unit"] else "unit missing"),
            })
    add("bid_schedule_totals", upsert(conn, "bid_schedule_totals", ["bid_id", "schedule"],
                                      ["bid_id", "schedule", "printed_total"], totals))
    add("bid_line_items", upsert(
        conn, "bid_line_items", ["bid_id", "line_no"],
        ["bid_id", "line_no", "schedule", "raw_item_code", "raw_description", "raw_unit",
         "quantity", "unit_price", "unit_code", "needs_review", "review_notes"], lines,
        set_exprs={"needs_review": "t.needs_review or excluded.needs_review",
                   "review_notes": "coalesce(t.review_notes, excluded.review_notes)"}))

    # Ranks, then the low bidder must match the workbook (SPEC Loader rule 6)
    conn.execute("select public.refresh_bid_ranks(%s)", (project_pk,))
    expected = [b["key"] for b in proj["bids"] if b["is_low_in_workbook"]]
    got = [r[0] for r in conn.execute(
        """select bd.name_normalized from public.bids b join public.bidders bd on bd.id = b.bidder_id
           where b.project_id = %s and b.is_low""", (project_pk,))]
    if got != expected:
        raise RuntimeError(f"{pid}: low bidder is {got}, workbook says {expected}")
    for key, rank in conn.execute(
            """select bd.name_normalized, b.rank from public.bids b join public.bidders bd on bd.id = b.bidder_id
               where b.project_id = %s""", (project_pk,)):
        wb_rank = next(b["workbook_rank"] for b in proj["bids"] if b["key"] == key) if key in keys else None
        if key in keys and wb_rank != rank:
            print(f"  NOTE {pid}: {bidder_names.get(key, key)} rank {rank}, workbook rank {wb_rank}")

    # Report (never delete) rows the workbook no longer has
    stale_bids = conn.execute(
        """select bd.name from public.bids b join public.bidders bd on bd.id = b.bidder_id
           where b.project_id = %s and not (bd.name_normalized = any(%s))""", (project_pk, keys)).fetchall()
    if stale_bids:
        print(f"  WARNING {pid}: bids not in the workbook (kept): {[r[0] for r in stale_bids]}")
    n_lines = {bid_ids[bidder_ids[b["key"]]]: len(b["lines"]) for b in proj["bids"]}
    stale_lines = sum(conn.execute(
        "select count(*) from public.bid_line_items where bid_id = %s and line_no > %s", (bid, n)).fetchone()[0]
        for bid, n in n_lines.items())
    if stale_lines:
        print(f"  WARNING {pid}: {stale_lines} line items beyond the workbook's rows (kept)")

    changes["_new_bidders"] = new_bidders
    changes["_low_bidder"] = got[0] if got else None
    changes["_bids"] = len(proj["bids"])
    changes["_lines"] = len(lines)
    return changes


def load(conn, projects, bidder_names, only=None, dry_run=False, verbose=True):
    """Load projects, one transaction each. Returns total Counter of changes."""
    units = {r[0] for r in conn.execute("select code from public.units")}
    total = Counter()
    for proj in projects:
        if only and proj["project_id"] not in only:
            continue
        with conn.transaction(force_rollback=dry_run):
            c = load_project(conn, proj, bidder_names, units)
        if verbose:
            ins = sum(v for k, v in c.items() if k.endswith(".inserted"))
            upd = sum(v for k, v in c.items() if k.endswith(".updated"))
            print(f"{proj['project_id']:<20} bids {c['_bids']:>2}  lines {c['_lines']:>5}  "
                  f"+{ins} ~{upd}  low: {bidder_names[c['_low_bidder']]}")
            for k in c["_new_bidders"]:
                print(f"  new bidder: {k:<45} {bidder_names[k]}")
        total.update({k: v for k, v in c.items() if not k.startswith("_")})
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bidders", action="store_true", help="print the bidder normalization table and stop")
    ap.add_argument("--project", action="append", help="load only this Project ID (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="load, then roll back every project")
    args = ap.parse_args()

    wb = load_workbook(WORKBOOK, read_only=True, data_only=True)
    projects, bidder_names, table = build_projects(wb)
    if args.bidders:
        print_bidder_table(table)
        return 0
    if args.project:
        unknown = set(args.project) - {p["project_id"] for p in projects}
        if unknown:
            raise SystemExit(f"not in the workbook: {sorted(unknown)}")

    load_dotenv(ROOT / ".env")
    if not os.environ.get("SUPABASE_DB_URL"):
        raise SystemExit("SUPABASE_DB_URL is not set in .env")
    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True) as conn:
        total = load(conn, projects, bidder_names, only=set(args.project or []), dry_run=args.dry_run)
    print("\nchanges:")
    for k in sorted(total):
        print(f"  {k:<32} {total[k]}")
    if args.dry_run:
        print("\nDRY RUN: every project rolled back, nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
