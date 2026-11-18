"""Load a cost index CSV from data/indices/ into cost_indices (PLAYBOOK Session 6).

CSV columns: date, value
  date   YYYY-MM-DD, or YYYY-MM (read as the first day of the month): the period the value applies to
  value  index value, > 0

Rows are upserted on (index_name, period); periods already in the database but not in the CSV
are reported, never deleted. With --activate, cost_settings.active_index_name is set to the index
so v_unit_prices_adjusted escalates prices with it (adjusted = unit price x latest value / value at
bid date). Without --activate, prices stay nominal.

The choice of index is an open engineering decision (SPEC.md decisions log: DEFERRED); loading one
does not activate it.

One transaction; validates the whole file first; idempotent.

Run:
  python scripts/load_cost_index.py nhcci.csv --name FHWA_NHCCI [--source "FHWA, 2026-11"] [--dry-run]
  python scripts/load_cost_index.py nhcci.csv --name FHWA_NHCCI --activate
A bare file name is looked up in data/indices/.
"""

import argparse
import csv
import os
import re
import sys
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
INDICES_DIR = ROOT / "data" / "indices"
_DATE = re.compile(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?")


def resolve(path):
    p = Path(path)
    return p if p.parent != Path(".") or p.exists() else INDICES_DIR / p


def parse_date(s):
    m = _DATE.fullmatch(s.strip())
    if not m:
        raise ValueError
    y, mo, d = int(m[1]), int(m[2]), int(m[3] or 1)
    return date(y, mo, d)


def read_index_csv(path):
    """Return [(period, value)] sorted by period. Raises ValueError listing every bad row."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = [c.strip().lower() for c in reader.fieldnames or []]
        if cols[:2] != ["date", "value"] and set(cols) != {"date", "value"}:
            raise ValueError(f"{Path(path).name}: columns must be date, value (got {reader.fieldnames})")
        rows = [{k.strip().lower(): (v or "").strip() for k, v in r.items()} for r in reader]
    errors, out, seen = [], [], set()
    for i, r in enumerate(rows, start=2):
        if not r["date"] and not r["value"]:
            continue
        problems = []
        try:
            period = parse_date(r["date"])
        except ValueError:
            problems.append(f"date {r['date']!r}")
            period = None
        try:
            value = Decimal(r["value"].replace(",", ""))
            if not value.is_finite() or value <= 0:
                raise InvalidOperation
        except InvalidOperation:
            problems.append(f"value {r['value']!r}")
            value = None
        if period in seen:
            problems.append(f"period {period} appears twice")
        if problems:
            errors.append(f"row {i}: " + ", ".join(problems))
            continue
        seen.add(period)
        out.append((period, value))
    if errors:
        raise ValueError(f"{len(errors)} invalid rows in {Path(path).name}:\n  " + "\n  ".join(errors))
    if not out:
        raise ValueError(f"{Path(path).name}: no rows")
    return sorted(out)


def load_index(conn, name, rows, source=None, activate=False):
    """Upsert rows into cost_indices inside the caller's transaction. Returns Counter of changes."""
    changes = Counter(inserted=0, updated=0)
    with conn.cursor() as cur:
        cur.executemany(
            """insert into public.cost_indices as t (index_name, period, value, source)
               values (%s, %s, %s, %s)
               on conflict (index_name, period) do update set value = excluded.value, source = excluded.source
               where (t.value, t.source) is distinct from (excluded.value, excluded.source)
               returning (xmax = 0)""",
            [(name, p, v, source) for p, v in rows], returning=True)
        while True:
            for (inserted,) in cur.fetchall():
                changes["inserted" if inserted else "updated"] += 1
            if not cur.nextset():
                break
    periods = {p for p, _ in rows}
    changes["not_in_csv"] = sum(1 for (p,) in conn.execute(
        "select period from public.cost_indices where index_name = %s", (name,)) if p not in periods)
    if activate:
        changes["activated"] = conn.execute(
            """update public.cost_settings set active_index_name = %s
               where id and active_index_name is distinct from %s""", (name, name)).rowcount
    return changes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("csv", help="file in data/indices/ (or a path)")
    ap.add_argument("--name", required=True, help="index_name to store it under, e.g. FHWA_NHCCI")
    ap.add_argument("--source", help="where the values came from (stored in cost_indices.source)")
    ap.add_argument("--activate", action="store_true", help="also set cost_settings.active_index_name")
    ap.add_argument("--dry-run", action="store_true", help="load and report, then roll back")
    args = ap.parse_args()

    path = resolve(args.csv)
    try:
        rows = read_index_csv(path)
    except (OSError, ValueError) as e:
        raise SystemExit(str(e))
    print(f"{path.name}: {len(rows)} periods, {rows[0][0]} .. {rows[-1][0]}, "
          f"values {min(v for _, v in rows)} .. {max(v for _, v in rows)}")

    load_dotenv(ROOT / ".env")
    if not os.environ.get("SUPABASE_DB_URL"):
        raise SystemExit("SUPABASE_DB_URL is not set in .env")
    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True) as conn:
        with conn.transaction(force_rollback=args.dry_run):
            changes = load_index(conn, args.name, rows, args.source, args.activate)
            active = conn.execute("select active_index_name from public.cost_settings where id").fetchone()[0]
            uncovered = conn.execute(
                """select count(*) from public.projects p where p.bid_date < (
                       select min(period) from public.cost_indices where index_name = %s)""",
                (args.name,)).fetchone()[0]
    print(f"cost_indices '{args.name}': {changes['inserted']} inserted, {changes['updated']} updated"
          + (f", {changes['not_in_csv']} periods in the database not in the CSV (kept)" if changes["not_in_csv"] else ""))
    if uncovered:
        print(f"WARNING: {uncovered} projects bid before the first period; their adjusted price is null "
              "while this index is active")
    print(f"active index: {active or 'none (prices nominal)'}")
    if args.dry_run:
        print("DRY RUN: rolled back, nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
