"""Load the reviewed pay-item catalog and match every bid line item (PLAYBOOK Session 4, part 2).

Source: data/review/catalog_candidates.csv after review. Each row is one distinct
(spec_section, description_clean, unit) found in the contractor line items, mapped by the
reviewer to (pay_item_spec_section, pay_item_name, unit) + category.

1. pay_items: one per distinct (pay_item_spec_section, pay_item_name, unit); category from the
   CSV; notes = the rows' distinct review_note values. Upsert on (spec_section, canonical_name,
   unit_code).
2. pay_item_aliases: one per CSV row, (description_clean, unit, spec_section or NULL) -> pay item,
   source = 'manual' (every row was reviewed by hand).
3. Matching: ingest/normalize.py over every bid_line_items row (contractors and engineer's
   estimates). Lines with match_method = 'manual' (set in the Session 5 review) are never touched.
   needs_review = unit missing/unknown, or the match needs review, or the CSV row's review_note
   starts with "CHECK".

One transaction; idempotent (unchanged rows are not rewritten). Nothing is deleted: pay items or
aliases in the database that are no longer in the CSV are reported.

Run:  python ingest/load_catalog.py [--dry-run]
"""

import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingest.load_master import upsert  # noqa: E402
from ingest.normalize import CatalogItem, match_line, normalize_line  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CATALOG_CSV = ROOT / "data" / "review" / "catalog_candidates.csv"
SECTION = re.compile(r"[A-Z]{1,5}-\d{3,4}|MISC")


# ---------------------------------------------------------------- CSV

def read_catalog(path=CATALOG_CSV, units=None):
    """Return (pay_items, aliases, check_notes).

    pay_items:   {(spec_section, canonical_name, unit_code): {"category", "notes"}}
    aliases:     {(description_normalized, unit_code, spec_section or None): pay item key}
    check_notes: {alias key: review_note} for rows whose note starts with CHECK
    Raises ValueError listing every invalid row.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    errors, pay_items, aliases, check_notes = [], {}, {}, {}
    notes = defaultdict(list)
    for i, r in enumerate(rows, start=2):                    # line 1 is the header
        r = {k: (v or "").strip() for k, v in r.items()}
        sec, name, unit, cat = r["pay_item_spec_section"], r["pay_item_name"], r["unit"], r["category"]
        problems = []
        if not SECTION.fullmatch(sec):
            problems.append(f"pay_item_spec_section {sec!r}")
        if not name:
            problems.append("pay_item_name blank")
        if not unit or (units is not None and unit not in units):
            problems.append(f"unit {unit!r}")
        if not r["description_clean"]:
            problems.append("description_clean blank")
        if problems:
            errors.append(f"row {i}: " + ", ".join(problems))
            continue
        item = (sec, name, unit)
        prev = pay_items.setdefault(item, {"category": cat or None})
        if prev["category"] != (cat or None):
            errors.append(f"row {i}: category {cat!r} differs from {prev['category']!r} for {item}")
        note = r.get("review_note", "")
        if note and note not in notes[item]:
            notes[item].append(note)
        key = (r["description_clean"], unit, r["spec_section"] or None)
        if key in aliases and aliases[key] != item:
            errors.append(f"row {i}: {key} mapped to both {aliases[key]} and {item}")
        aliases[key] = item
        if note.upper().startswith("CHECK"):
            check_notes[key] = note
    if errors:
        raise ValueError(f"{len(errors)} invalid rows in {path.name}:\n  " + "\n  ".join(errors))
    for item, v in pay_items.items():
        v["notes"] = "; ".join(notes[item]) or None
    return pay_items, aliases, check_notes


# ---------------------------------------------------------------- matching

def plan_matches(lines, catalog, aliases, places, check_notes):
    """Compute (pay_item_id, match_method, match_score, needs_review, review_notes) per line id.

    lines: rows of (id, raw_item_code, raw_description, unit_code, review_notes, match_method)
    """
    names = {c.id: c.canonical_name for c in catalog}
    cache, out = {}, {}
    for lid, code, desc, unit, old_notes, old_method in lines:
        if old_method == "manual":
            continue
        n = normalize_line(code, desc, unit, places)
        if n not in cache:
            cache[n] = match_line(n, catalog, aliases)
        m = cache[n]
        if unit is None:                                     # loader's unit note stays
            review, note = True, old_notes or "unit missing"
        else:
            check = check_notes.get((n.description, n.unit, n.spec_section)) if m.method == "alias" else None
            review = m.needs_review or check is not None
            note = check or (_match_note(m, n, names) if m.needs_review else None)
        out[lid] = (m.pay_item_id, m.method,
                    None if m.score is None else Decimal(str(round(m.score, 4))), review, note)
    return out


def _match_note(m, n, names):
    if m.method == "unmatched":
        return "no catalog item" + (f" (best fuzzy {m.score:.2f})" if m.score is not None else f" for unit {n.unit}")
    return f"{m.method} {m.score:.2f} -> {names[m.pay_item_id]}"


# ---------------------------------------------------------------- database

def load(conn, pay_items, aliases, check_notes, verbose=True):
    """Write catalog + matches inside the caller's transaction. Returns Counter of changes."""
    changes = Counter()
    say = print if verbose else (lambda *a, **k: None)

    c = upsert(conn, "pay_items", ["spec_section", "canonical_name", "unit_code"],
               ["spec_section", "canonical_name", "unit_code", "category", "notes"],
               [{"spec_section": s, "canonical_name": n, "unit_code": u, **v}
                for (s, n, u), v in pay_items.items()])
    changes.update({f"pay_items.{k}": v for k, v in c.items()})

    ids = {(s, n, u): i for i, s, n, u in conn.execute(
        "select id, spec_section, canonical_name, unit_code from public.pay_items")}
    c = upsert(conn, "pay_item_aliases", ["description_normalized", "unit_code", "spec_section"],
               ["description_normalized", "unit_code", "spec_section", "pay_item_id", "source"],
               [{"description_normalized": d, "unit_code": u, "spec_section": s,
                 "pay_item_id": ids[item], "source": "manual"} for (d, u, s), item in aliases.items()])
    changes.update({f"pay_item_aliases.{k}": v for k, v in c.items()})

    stale = set(ids) - set(pay_items)
    if stale:
        say(f"WARNING: {len(stale)} pay items in the database are not in the CSV (not deleted):")
        for s, n, u in sorted(stale):
            say(f"  {s} | {n} | {u}")

    catalog = [CatalogItem(i, s, n, u) for (s, n, u), i in ids.items()]
    db_aliases = {(d, u, s): pid for d, u, s, pid in conn.execute(
        "select description_normalized, unit_code, spec_section, pay_item_id from public.pay_item_aliases")}
    places = [r[0] for r in conn.execute(
        """select city from public.airports where city is not null
           union select location from public.projects where location is not null""")]
    lines = conn.execute("""select id, raw_item_code, raw_description, unit_code, review_notes, match_method
                            from public.bid_line_items""").fetchall()
    current = {r[0]: r[1:] for r in conn.execute(
        """select id, pay_item_id, match_method, match_score, needs_review, review_notes
           from public.bid_line_items""")}

    planned = plan_matches(lines, catalog, db_aliases, places, check_notes)
    updates = [(*v, lid) for lid, v in planned.items() if tuple(current[lid]) != v]
    with conn.cursor() as cur:
        cur.executemany("""update public.bid_line_items
                           set pay_item_id = %s, match_method = %s, match_score = %s,
                               needs_review = %s, review_notes = %s
                           where id = %s""", updates)
    changes["bid_line_items.updated"] = len(updates)
    return changes


def report(conn):
    print("\nmatch counts by method (all line items):")
    rows = conn.execute("""
        select coalesce(li.match_method, '(none)'), b.is_estimate, li.needs_review, count(*)
        from public.bid_line_items li join public.bids b on b.id = li.bid_id
        group by 1, 2, 3 order by 1, 2, 3""").fetchall()
    table = defaultdict(Counter)
    for method, est, review, n in rows:
        table[method]["estimate" if est else "contractor"] += n
        table[method]["needs_review"] += n if review else 0
    print(f"  {'method':<10} {'contractor':>10} {'estimate':>9} {'total':>7} {'needs_review':>13}")
    for method, t in sorted(table.items()):
        print(f"  {method:<10} {t['contractor']:>10} {t['estimate']:>9} "
              f"{t['contractor'] + t['estimate']:>7} {t['needs_review']:>13}")
    tot = Counter()
    for t in table.values():
        tot.update(t)
    print(f"  {'total':<10} {tot['contractor']:>10} {tot['estimate']:>9} "
          f"{tot['contractor'] + tot['estimate']:>7} {tot['needs_review']:>13}")

    n_queue = conn.execute("select count(*) from public.v_review_queue").fetchone()[0]
    print(f"\nv_review_queue: {n_queue} rows")
    for why, n in conn.execute("""
            select case when unit_code is null then 'unit missing/unknown'
                        when review_notes ilike 'CHECK%%' then 'flagged CHECK in catalog review'
                        else coalesce(match_method, '(none)') end, count(*)
            from public.bid_line_items where needs_review or pay_item_id is null
            group by 1 order by 2 desc"""):
        print(f"  {why:<32} {n}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="load and report, then roll back")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    if not os.environ.get("SUPABASE_DB_URL"):
        raise SystemExit("SUPABASE_DB_URL is not set in .env")
    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True) as conn:
        units = {r[0] for r in conn.execute("select code from public.units")}
        try:
            pay_items, aliases, check_notes = read_catalog(units=units)
        except ValueError as e:
            raise SystemExit(str(e))
        print(f"{CATALOG_CSV.relative_to(ROOT)}: {len(aliases)} rows -> {len(pay_items)} pay items, "
              f"{len(check_notes)} rows flagged CHECK")
        with conn.transaction(force_rollback=args.dry_run):
            changes = load(conn, pay_items, aliases, check_notes)
            print("\nchanges:")
            for k in sorted(changes):
                print(f"  {k:<32} {changes[k]}")
            report(conn)
    if args.dry_run:
        print("\nDRY RUN: rolled back, nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
