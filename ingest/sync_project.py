"""Load one workbook project into the cost database and match its line items (PLAYBOOK Session 7).

Called by main.py after it upserts a new-format project into output/master_unit_prices.xlsx
(skip with main.py --no-db), and runnable by hand to retry:

  1. ingest/load_master.py logic for this Project ID only (bids, line items, ranks; same rules,
     idempotent, never deletes, never creates airports);
  2. the pay-item normalizer (ingest/load_catalog.match_line_items) over this project's lines,
     against the catalog and aliases already in the database. Lines matched by hand
     (match_method = 'manual') are left alone; unmatched or review-band lines get needs_review
     and appear in v_review_queue (then: scripts/export_review_queue.py, scripts/apply_review.py).

Both steps run in one transaction. The workbook is only read.

Run:  python ingest/sync_project.py NTB2614LHART00034 [--dry-run]
"""

import argparse
import csv
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class SyncError(Exception):
    """The project could not be loaded; the message says what to fix."""


def _airports_mapped(project_id):
    from ingest.load_master import PROJECT_AIRPORTS_CSV
    with open(PROJECT_AIRPORTS_CSV, newline="", encoding="utf-8-sig") as f:
        return any(r["project_id"] == project_id and r["faa_id"].strip() for r in csv.DictReader(f))


def sync_project(project_id, dry_run=False, verbose=True, db_url=None):
    """Load + match one project. Returns a summary dict; raises SyncError with a fix-it message."""
    import psycopg
    from dotenv import load_dotenv
    from openpyxl import load_workbook

    from ingest.load_catalog import CATALOG_CSV, match_line_items, read_catalog
    from ingest.load_master import WORKBOOK, build_projects, load

    retry = f"python ingest/sync_project.py {project_id}"
    if not _airports_mapped(project_id):
        raise SyncError(f"{project_id} is not in data/project_airports.csv (the loader never creates airports). "
                        f"Add its FAA id(s) there, then run: {retry}")
    load_dotenv(ROOT / ".env")
    db_url = db_url or os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        raise SyncError("SUPABASE_DB_URL is not set in .env (use main.py --no-db for a workbook-only run)")

    try:
        projects, bidder_names, _ = build_projects(load_workbook(WORKBOOK, read_only=True, data_only=True))
    except SystemExit as e:                     # build_projects reports workbook problems this way
        raise SyncError(f"workbook check failed: {e}") from None
    if project_id not in {p["project_id"] for p in projects}:
        raise SyncError(f"{project_id} is not in {WORKBOOK.name}")

    with psycopg.connect(db_url, autocommit=True) as conn:
        units = {r[0] for r in conn.execute("select code from public.units")}
        _, _, check_notes = read_catalog(CATALOG_CSV, units=units)
        try:
            with conn.transaction(force_rollback=dry_run):
                changes = load(conn, projects, bidder_names, only={project_id}, verbose=verbose)
                matched = match_line_items(conn, check_notes, project_id)
                methods = Counter(dict(conn.execute("""
                    select coalesce(li.match_method, '(none)'), count(*)
                    from public.bid_line_items li join public.bids b on b.id = li.bid_id
                    join public.projects p on p.id = b.project_id
                    where p.project_id = %s group by 1""", (project_id,)).fetchall()))
                queue = conn.execute("select count(*) from public.v_review_queue where project_id = %s",
                                     (project_id,)).fetchone()[0]
        except RuntimeError as e:               # load_project: unknown airport, low-bidder mismatch
            raise SyncError(f"{e}. Nothing was written. Fix it, then run: {retry}") from None

    return {"project_id": project_id, "changes": changes, "lines_rematched": matched,
            "match_methods": methods, "review_queue": queue, "dry_run": dry_run}


def describe(summary):
    c = summary["changes"]
    ins = sum(v for k, v in c.items() if k.endswith(".inserted"))
    upd = sum(v for k, v in c.items() if k.endswith(".updated"))
    methods = ", ".join(f"{k} {v}" for k, v in sorted(summary["match_methods"].items()))
    return (f"{summary['project_id']}: database {ins} inserted, {upd} updated; "
            f"{summary['lines_rematched']} line matches changed ({methods}); "
            f"{summary['review_queue']} lines in v_review_queue"
            + (" [dry run, rolled back]" if summary["dry_run"] else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("project_id")
    ap.add_argument("--dry-run", action="store_true", help="load and match, then roll back")
    args = ap.parse_args()
    try:
        summary = sync_project(args.project_id, dry_run=args.dry_run)
    except SyncError as e:
        raise SystemExit(f"ERROR: {e}")
    print(describe(summary))
    if summary["review_queue"]:
        print("review them: python scripts/export_review_queue.py, then scripts/apply_review.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
