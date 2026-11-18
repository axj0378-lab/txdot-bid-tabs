"""Mirror pc_airport (Pavement Condition Database) into our airports table.

1. Read pc_airport through PCD_DB_URL. Read-only: the session is READ ONLY and
   only SELECTs are run. Never write to the Pavement Condition Database.
2. Upsert into airports on faa_id with region = txdot_district.
3. Apply data/airport_overrides.csv:
   - source = pc_airport rows only fill a missing txdot_district / region
     (a disagreement with an existing value is printed, not applied);
   - source = manual rows are upserted whole (e.g. FWS, Fort Worth Spinks).
4. Check: every faa_id in data/project_airports.csv exists in airports, and
   list airports with no region. Exit code 1 if a project airport is missing
   or has no region.

Steps 2-3 run in one transaction. Mirrored airports that are no longer in
pc_airport are reported, never deleted.

Run:  python scripts/sync_airports.py [--dry-run]
"""

import argparse
import csv
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
OVERRIDES_CSV = ROOT / "data" / "airport_overrides.csv"
PROJECT_AIRPORTS_CSV = ROOT / "data" / "project_airports.csv"

PC_COLUMNS = ["id", "faa_id", "name", "city", "txdot_district", "latitude", "longitude"]


def _blank_to_none(value):
    value = (value or "").strip()
    return value or None


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k: _blank_to_none(v) for k, v in row.items()} for row in csv.DictReader(f)]


def read_pc_airports(pcd_url):
    """All pc_airport rows, through a read-only session."""
    with psycopg.connect(pcd_url) as conn:
        conn.execute("set session characteristics as transaction read only")
        cur = conn.execute(f"select {', '.join(PC_COLUMNS)} from public.pc_airport order by faa_id")
        rows = [dict(zip(PC_COLUMNS, r)) for r in cur.fetchall()]
        conn.rollback()
    for r in rows:
        r["faa_id"] = r["faa_id"].strip().upper()
        r["txdot_district"] = _blank_to_none(r["txdot_district"])
    return rows


def upsert_mirror(conn, pc_rows):
    conn.cursor().executemany(
        """insert into public.airports
             (faa_id, pc_airport_id, name, city, txdot_district, region, latitude, longitude, source, synced_at)
           values (%(faa_id)s, %(id)s, %(name)s, %(city)s, %(txdot_district)s, %(txdot_district)s,
                   %(latitude)s, %(longitude)s, 'pc_airport', now())
           on conflict (faa_id) do update set
             pc_airport_id  = excluded.pc_airport_id,
             name           = excluded.name,
             city           = excluded.city,
             txdot_district = excluded.txdot_district,
             region         = excluded.region,
             latitude       = excluded.latitude,
             longitude      = excluded.longitude,
             source         = 'pc_airport',
             synced_at      = now()""",
        pc_rows,
    )


def apply_overrides(conn, overrides):
    for o in overrides:
        faa = o["faa_id"].upper()
        if o["source"] == "manual":
            if not o["name"]:
                raise SystemExit(f"{OVERRIDES_CSV.name}: manual airport {faa} needs a name")
            conn.execute(
                """insert into public.airports (faa_id, name, city, txdot_district, region, source, synced_at)
                   values (%s, %s, %s, %s, coalesce(%s, %s), 'manual', now())
                   on conflict (faa_id) do update set
                     name = excluded.name, city = excluded.city,
                     txdot_district = excluded.txdot_district, region = excluded.region,
                     source = 'manual', synced_at = now()
                   where airports.source = 'manual'""",
                (faa, o["name"], o["city"], o["txdot_district"], o["region"], o["txdot_district"]),
            )
            src = conn.execute("select source from public.airports where faa_id = %s", (faa,)).fetchone()[0]
            if src != "manual":
                print(f"  WARNING {faa}: listed as manual but now mirrored from pc_airport; override skipped")
            continue

        row = conn.execute(
            "select txdot_district, region from public.airports where faa_id = %s", (faa,)
        ).fetchone()
        if row is None:
            print(f"  WARNING {faa}: override for an airport not in pc_airport; not created")
            continue
        for col, current in zip(("txdot_district", "region"), row):
            wanted = o[col]
            if wanted and current and current != wanted:
                print(f"  WARNING {faa}: {col} is {current!r}, override says {wanted!r}; kept {current!r}")
        conn.execute(
            """update public.airports
               set txdot_district = coalesce(txdot_district, %s),
                   region         = coalesce(region, %s)
               where faa_id = %s""",
            (o["txdot_district"], o["region"], faa),
        )


def check(conn, pc_faa_ids, project_airports):
    airports = {
        faa: (region, source)
        for faa, region, source in conn.execute("select faa_id, region, source from public.airports")
    }
    used = sorted({r["faa_id"].upper() for r in project_airports})

    missing = [f for f in used if f not in airports]
    used_no_region = [f for f in used if f in airports and not airports[f][0]]
    other_no_region = sorted(f for f, (region, _) in airports.items() if not region and f not in used)
    stale = sorted(f for f, (_, src) in airports.items() if src == "pc_airport" and f not in pc_faa_ids)
    n_manual = sum(1 for _, src in airports.values() if src == "manual")

    print(f"\nairports: {len(airports)} rows ({len(airports) - n_manual} pc_airport, {n_manual} manual)")
    print(f"project airports: {len(used)} distinct in {PROJECT_AIRPORTS_CSV.name}")
    print(f"  missing from airports: {', '.join(missing) or 'none'}")
    print(f"  without a region:      {', '.join(used_no_region) or 'none'}")
    print(f"other airports without a region ({len(other_no_region)}): {', '.join(other_no_region) or 'none'}")
    if stale:
        print(f"mirrored airports no longer in pc_airport (not deleted): {', '.join(stale)}")
    return not missing and not used_no_region


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="do everything, then roll back")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    for var in ("PCD_DB_URL", "SUPABASE_DB_URL"):
        if not os.environ.get(var):
            raise SystemExit(f"{var} is not set in .env")

    pc_rows = read_pc_airports(os.environ["PCD_DB_URL"])
    print(f"pc_airport: {len(pc_rows)} rows read (read-only)")
    overrides = read_csv(OVERRIDES_CSV)
    project_airports = read_csv(PROJECT_AIRPORTS_CSV)

    with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
        upsert_mirror(conn, pc_rows)
        print(f"upserted {len(pc_rows)} mirrored airports")
        print(f"applying {len(overrides)} overrides from {OVERRIDES_CSV.name}")
        apply_overrides(conn, overrides)
        ok = check(conn, {r["faa_id"] for r in pc_rows}, project_airports)
        if args.dry_run:
            conn.rollback()
            print("\nDRY RUN: rolled back, nothing written")
        else:
            conn.commit()
            print("\ncommitted")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
