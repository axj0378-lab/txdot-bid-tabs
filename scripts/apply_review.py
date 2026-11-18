"""Apply a filled-in data/review/queue.csv (PLAYBOOK Session 5).

Per row (rows with no decision are skipped):
  confirmed_pay_item_id = N         line -> pay item N; match_method = 'manual', needs_review cleared;
                                    adds a pay_item_aliases row (source 'manual') for the line's
                                    normalized (description, unit, section) so later loads match it
  exclude = Y                       line stays in the bid and in total checks but leaves price
                                    statistics and the queue (excluded_from_stats); keeps its
                                    current pay item unless confirmed_pay_item_id is also given;
                                    no alias is added
  reviewer_note                     stored in review_notes (otherwise the existing note stays)

The confirmed pay item's unit must equal the line's unit. An existing alias for the same key that
points to a different pay item is not changed; it is reported (it came from catalog_candidates.csv,
so fix that file and re-run ingest/load_catalog.py to remap every line using it).

Lines set here (match_method = 'manual') are never re-matched by ingest/load_catalog.py.
One transaction; validates the whole file first and writes nothing if any row is invalid.
Idempotent: re-applying the same file changes nothing.

Run:  python scripts/apply_review.py [--file data/review/queue.csv] [--dry-run]
"""

import argparse
import csv
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingest.load_catalog import read_places  # noqa: E402
from ingest.normalize import normalize_line  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
QUEUE_CSV = ROOT / "data" / "review" / "queue.csv"
YES = {"Y", "YES", "X", "TRUE", "1"}
NO = {"", "N", "NO", "FALSE", "0"}


@dataclass(frozen=True)
class Decision:
    line_item_id: int
    pay_item_id: int | None      # None: exclude and keep the current pay item
    exclude: bool
    note: str | None


def parse_decisions(rows, lines, pay_item_units):
    """Validate filled-in queue rows. Returns (decisions, errors).

    rows:            dicts from csv.DictReader (header on line 1)
    lines:           {line_item_id: (unit_code, current pay_item_id)}
    pay_item_units:  {pay_item_id: unit_code}
    """
    decisions, errors, seen = [], [], set()
    for i, r in enumerate(rows, start=2):
        get = lambda k: (r.get(k) or "").strip()                    # noqa: E731
        raw_id, raw_ex, note = get("confirmed_pay_item_id"), get("exclude").upper(), get("reviewer_note")
        if not raw_id and raw_ex in NO:
            if note:
                errors.append(f"row {i}: reviewer_note without a decision")
            continue
        problems = []
        try:
            lid = int(get("line_item_id"))
        except ValueError:
            errors.append(f"row {i}: line_item_id {get('line_item_id')!r}")
            continue
        if lid not in lines:
            problems.append(f"line_item_id {lid} not found")
        elif lid in seen:
            problems.append(f"line_item_id {lid} appears twice")
        seen.add(lid)
        if raw_ex not in YES | NO:
            problems.append(f"exclude {get('exclude')!r} (use Y or leave blank)")
        pid = None
        if raw_id:
            try:
                pid = int(float(raw_id))           # Excel may save 123 as 123.0
            except ValueError:
                problems.append(f"confirmed_pay_item_id {raw_id!r}")
            else:
                if pid not in pay_item_units:
                    problems.append(f"pay item {pid} not found")
                elif lid in lines and pay_item_units[pid] != lines[lid][0]:
                    problems.append(f"pay item {pid} is {pay_item_units[pid]}, line is {lines[lid][0]}")
        if problems:
            errors.append(f"row {i}: " + ", ".join(problems))
            continue
        exclude = raw_ex in YES
        decisions.append(Decision(lid, pid if pid is not None else lines[lid][1], exclude, note or None))
    return decisions, errors


def apply(conn, decisions, verbose=True):
    """Write decisions inside the caller's transaction. Returns Counter of changes."""
    say = print if verbose else (lambda *a, **k: None)
    changes = Counter()
    ids = [d.line_item_id for d in decisions]
    cur_rows = {r[0]: r[1:] for r in conn.execute(
        """select id, pay_item_id, match_method, match_score, needs_review, excluded_from_stats, review_notes,
                  raw_item_code, raw_description, unit_code
           from public.bid_line_items where id = any(%s)""", (ids,))}
    places = read_places(conn)

    updates, alias_keys = [], {}
    for d in decisions:
        pid, method, score, review, excl, notes, code, desc, unit = cur_rows[d.line_item_id]
        new = (d.pay_item_id, "manual", None, False, d.exclude, d.note or notes)
        if (pid, method, score, review, excl, notes) != new:
            updates.append((*new, d.line_item_id))
            changes["excluded" if d.exclude else "confirmed"] += 1
        if not d.exclude:
            n = normalize_line(code, desc, unit, places)
            alias_keys.setdefault((n.description, n.unit, n.spec_section), set()).add(d.pay_item_id)

    with conn.cursor() as cur:
        cur.executemany("""update public.bid_line_items
                           set pay_item_id = %s, match_method = %s, match_score = %s, needs_review = %s,
                               excluded_from_stats = %s, review_notes = %s
                           where id = %s""", updates)
    changes["bid_line_items.updated"] = len(updates)

    existing = {(d, u, s): p for d, u, s, p in conn.execute(
        "select description_normalized, unit_code, spec_section, pay_item_id from public.pay_item_aliases")}
    new_aliases = []
    for key, pids in sorted(alias_keys.items(), key=str):
        if len(pids) > 1:
            say(f"NOTE: no alias added for {key}: confirmed to several pay items {sorted(pids)}")
            changes["aliases.ambiguous"] += 1
            continue
        pid = next(iter(pids))
        if key not in existing:
            new_aliases.append({"d": key[0], "u": key[1], "s": key[2], "p": pid})
        elif existing[key] != pid:
            say(f"NOTE: alias {key} points to pay item {existing[key]}, kept (confirmed {pid}); "
                "change it in catalog_candidates.csv to remap every line using it")
            changes["aliases.conflict"] += 1
    with conn.cursor() as cur:
        cur.executemany("""insert into public.pay_item_aliases
                             (description_normalized, unit_code, spec_section, pay_item_id, source)
                           values (%(d)s, %(u)s, %(s)s, %(p)s, 'manual')""", new_aliases)
    changes["pay_item_aliases.inserted"] = len(new_aliases)
    return changes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", type=Path, default=QUEUE_CSV)
    ap.add_argument("--dry-run", action="store_true", help="apply and report, then roll back")
    args = ap.parse_args()
    with open(args.file, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    load_dotenv(ROOT / ".env")
    if not os.environ.get("SUPABASE_DB_URL"):
        raise SystemExit("SUPABASE_DB_URL is not set in .env")
    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True) as conn:
        lines = {r[0]: r[1:] for r in conn.execute("select id, unit_code, pay_item_id from public.bid_line_items")}
        units = dict(conn.execute("select id, unit_code from public.pay_items").fetchall())
        decisions, errors = parse_decisions(rows, lines, units)
        if errors:
            raise SystemExit(f"{len(errors)} invalid rows in {args.file.name}, nothing written:\n  "
                             + "\n  ".join(errors))
        print(f"{args.file.name}: {len(rows)} rows, {len(decisions)} decisions "
              f"({sum(not d.exclude for d in decisions)} confirm, {sum(d.exclude for d in decisions)} exclude), "
              f"{len(rows) - len(decisions)} left blank")
        with conn.transaction(force_rollback=args.dry_run):
            changes = apply(conn, decisions)
            print("\nchanges:")
            for k in sorted(changes):
                print(f"  {k:<28} {changes[k]}")
            left = conn.execute("select count(*) from public.v_review_queue").fetchone()[0]
            excluded = conn.execute(
                "select count(*) from public.bid_line_items where excluded_from_stats").fetchone()[0]
            print(f"\nv_review_queue: {left} rows left; {excluded} lines excluded from price statistics")
    if args.dry_run:
        print("\nDRY RUN: rolled back, nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
