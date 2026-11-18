"""Build data/review/catalog_candidates.csv for the pay-item catalog review (PLAYBOOK Session 4).

One row per distinct (spec_section, cleaned description, unit) across all contractor line items
(engineer's estimates excluded), with counts, price spread, an example of the raw text, and
suggested values for the columns the reviewer edits:

  pay_item_spec_section  section of the pay item (required; suggested for rows without one)
  pay_item_name          canonical name; rows with the same section + name + unit become one pay item
  category               paving, marking, lighting, drainage, fencing, turf, general, ... (suggested
                         from the FAA AC 150/5370-10 series letter only)

Read-only against the database. Writes only the CSV.

Run:  python ingest/catalog_candidates.py
"""

import csv
import os
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingest.normalize import item_key, normalize_line  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "review" / "catalog_candidates.csv"

SERIES_CATEGORY = {"C": "general", "G": "general", "P": "paving", "D": "drainage",
                   "F": "fencing", "T": "turf", "L": "lighting"}

COLUMNS = ["spec_section", "description_clean", "unit", "n_lines", "n_projects", "n_bidders",
           "median_unit_price", "min_unit_price", "max_unit_price", "has_credits",
           "example_raw_code", "example_raw_description", "projects",
           "section_suggested", "pay_item_spec_section", "pay_item_name", "category"]


def suggest_category(section):
    if not section:
        return ""
    if section == "P-620":
        return "marking"
    prefix = section.split("-")[0]
    return SERIES_CATEGORY.get(prefix, "")


def read_lines(conn):
    places = [r[0] for r in conn.execute(
        """select city from public.airports where city is not null
           union select location from public.projects where location is not null""")]
    lines = conn.execute("""
        select li.raw_item_code, li.raw_description, li.unit_code, li.quantity, li.unit_price,
               p.project_id, b.bidder_id
        from public.bid_line_items li
        join public.bids b     on b.id = li.bid_id
        join public.projects p on p.id = b.project_id
        where not b.is_estimate""").fetchall()
    return places, lines


def build_candidates(places, lines):
    groups = defaultdict(lambda: {"lines": 0, "projects": set(), "bidders": set(), "prices": [],
                                  "credits": False, "examples": Counter()})
    for code, desc, unit, qty, price, project, bidder in lines:
        n = normalize_line(code, desc, unit, places)
        g = groups[(n.spec_section, n.description, n.unit)]
        g["lines"] += 1
        g["projects"].add(project)
        g["bidders"].add(bidder)
        if price is not None and qty is not None and qty > 0:
            g["prices"].append(float(price))
        g["credits"] |= qty is not None and qty < 0
        g["examples"][(code, desc)] += 1

    # Suggest a section for rows without one, from a twin with the same item_key and unit.
    sectioned = defaultdict(Counter)        # (item_key, unit) -> Counter(section -> lines)
    for (sec, desc, unit), g in groups.items():
        if sec:
            sectioned[(item_key(desc), unit)][sec] += g["lines"]
    suggested = {}
    for key in groups:
        sec, desc, unit = key
        twins = sectioned.get((item_key(desc), unit))
        if not sec and twins:
            suggested[key] = twins.most_common(1)[0][0]

    # Suggest one name per item_key within (section, unit): the description with the most lines.
    names, reps = {}, {}
    for key in sorted(groups, key=lambda k: (-groups[k]["lines"], k[1])):
        sec, desc, unit = key
        cluster = (sec or suggested.get(key), unit, item_key(desc))
        names[key] = reps.setdefault(cluster, desc)

    rows = []
    for key, g in groups.items():
        sec, desc, unit = key
        pay_sec = sec or suggested.get(key, "")
        (ex_code, ex_desc), _ = g["examples"].most_common(1)[0]
        prices = g["prices"]
        rows.append({
            "spec_section": sec or "",
            "description_clean": desc,
            "unit": unit or "",
            "n_lines": g["lines"],
            "n_projects": len(g["projects"]),
            "n_bidders": len(g["bidders"]),
            "median_unit_price": round(statistics.median(prices), 2) if prices else "",
            "min_unit_price": round(min(prices), 2) if prices else "",
            "max_unit_price": round(max(prices), 2) if prices else "",
            "has_credits": "Y" if g["credits"] else "",
            "example_raw_code": ex_code or "",
            "example_raw_description": ex_desc or "",
            "projects": " ".join(sorted(g["projects"])),
            "section_suggested": "Y" if not sec and pay_sec else "",
            "pay_item_spec_section": pay_sec,
            "pay_item_name": names[key],
            "category": suggest_category(pay_sec),
        })
    rows.sort(key=lambda r: (r["pay_item_spec_section"] or "~", r["pay_item_name"], r["unit"], -r["n_lines"]))
    return rows


def main():
    load_dotenv(ROOT / ".env")
    if not os.environ.get("SUPABASE_DB_URL"):
        raise SystemExit("SUPABASE_DB_URL is not set in .env")
    with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
        conn.execute("set session characteristics as transaction read only")
        places, lines = read_lines(conn)
    rows = build_candidates(places, lines)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:   # BOM so Excel opens it as UTF-8
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    n_lines = sum(r["n_lines"] for r in rows)
    no_sec = [r for r in rows if not r["spec_section"]]
    print(f"{len(lines)} contractor line items -> {len(rows)} candidates ({n_lines} lines)")
    print(f"  without a section in the code: {len(no_sec)} candidates "
          f"({sum(r['n_lines'] for r in no_sec)} lines); section suggested for "
          f"{sum(1 for r in no_sec if r['section_suggested'])}, still blank for "
          f"{sum(1 for r in no_sec if not r['pay_item_spec_section'])}")
    names = {(r["pay_item_spec_section"], r["pay_item_name"], r["unit"]) for r in rows}
    print(f"  suggested pay items (distinct section + name + unit): {len(names)}")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
