"""Write data/review/spot_check.csv: every contractor line behind the statewide median of the
given pay items, to check by hand against the workbook (PLAYBOOK Session 6).

status per line:
  kept               counts in v_unit_price_stats (STATEWIDE)
  dropped_outlier    outside [median / k, median x k], k = cost_settings.outlier_multiple
  not_in_stats       never eligible; reason says why (credit, lump sum, excluded, ...)

The script repeats the views' filters and checks that the kept lines reproduce the STATEWIDE
n_line_items and median exactly; it stops if they differ. Read-only.

Run:  python scripts/spot_check.py 827 564 682 831 763
"""

import argparse
import csv
import os
import statistics
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "review" / "spot_check.csv"

LINES_SQL = """
    select li.pay_item_id, pi.spec_section, pi.canonical_name, pi.unit_code,
           p.project_id, p.bid_date, bd.name, b.is_low, li.schedule, li.line_no,
           li.raw_item_code, li.raw_description, li.raw_unit, li.quantity, li.unit_price, li.extended_price,
           v.adjusted_unit_price, v.region, v.in_award_schedule, v.line_item_id is not null as in_adjusted,
           u.is_lump_sum, li.needs_review, li.excluded_from_stats, b.is_responsive, li.unit_code = pi.unit_code
    from public.bid_line_items li
    join public.bids b      on b.id = li.bid_id
    join public.bidders bd  on bd.id = b.bidder_id
    join public.projects p  on p.id = b.project_id
    join public.pay_items pi on pi.id = li.pay_item_id
    join public.units u     on u.code = pi.unit_code
    left join public.v_unit_prices_adjusted v on v.line_item_id = li.id
    where li.pay_item_id = any(%s) and not b.is_estimate
    order by array_position(%s, li.pay_item_id), p.project_id, li.schedule, li.line_no, bd.name"""

COLUMNS = ["pay_item_id", "spec_section", "pay_item_name", "unit", "project_id", "bid_date", "region",
           "bidder", "is_low", "schedule", "line_no", "raw_item_code", "raw_description", "raw_unit",
           "quantity", "unit_price", "extended_price", "statewide_median", "status", "reason"]


def not_eligible_reason(r, settings):
    (*_, adjusted, region, in_award, in_adjusted, lump_sum, review, excluded, responsive, unit_ok) = r
    quantity, unit_price, is_low = r[13], r[14], r[7]
    if excluded:
        return "excluded in review"
    if review:
        return "needs review"
    if not responsive:
        return "bid not responsive"
    if not unit_ok:
        return "unit differs from pay item"
    if unit_price is None:
        return "no unit price"
    if quantity <= 0:
        return "credit (negative quantity)"
    if not in_adjusted:
        return "not in v_unit_prices_adjusted"
    if lump_sum:
        return "lump sum"
    if adjusted is None or adjusted <= 0:
        return "zero or no adjusted price"
    if settings["stats_bidders"] == "low_only" and not is_low:
        return "not low bidder (stats_bidders = low_only)"
    if not settings["include_alternates"] and not in_award:
        return "alternate schedule (include_alternates = false)"
    return None


def build(conn, ids):
    settings = dict(zip(("stats_bidders", "include_alternates", "k"), conn.execute(
        "select stats_bidders, include_alternates, outlier_multiple::float8 from public.cost_settings where id"
    ).fetchone()))
    lines = conn.execute(LINES_SQL, (ids, ids)).fetchall()
    stats = {r[0]: r[1:] for r in conn.execute(
        """select pay_item_id, n_line_items, median from public.v_unit_price_stats
           where region = 'STATEWIDE' and pay_item_id = any(%s)""", (ids,))}
    rows = []
    for pid in ids:
        mine = [r for r in lines if r[0] == pid]
        if not mine:
            raise SystemExit(f"pay item {pid}: no contractor lines")
        reasons = [not_eligible_reason(r, settings) for r in mine]
        prices = [float(r[16]) for r, why in zip(mine, reasons) if why is None]
        m = statistics.median(prices) if prices else None
        kept = [m is not None and m / settings["k"] <= float(r[16]) <= m * settings["k"] if why is None else False
                for r, why in zip(mine, reasons)]
        kept_prices = [float(r[16]) for r, k in zip(mine, kept) if k]
        median = round(Decimal(statistics.median(kept_prices)), 4) if kept_prices else None
        n_view, med_view = stats.get(pid, (0, None))
        if len(kept_prices) != n_view or median != med_view:
            raise SystemExit(f"pay item {pid}: recomputed n={len(kept_prices)} median={median} "
                             f"differs from v_unit_price_stats n={n_view} median={med_view}")
        for r, why, k in zip(mine, reasons, kept):
            status = "kept" if k else "not_in_stats" if why else "dropped_outlier"
            if status == "dropped_outlier":
                why = f"price {float(r[16]):g} outside {m / settings['k']:.2f}-{m * settings['k']:.2f} (pre-filter median {m:g})"
            rows.append([pid, r[1], r[2], r[3], r[4], r[5], r[17] or "", r[6], "Y" if r[7] else "",
                         r[8], r[9], r[10], r[11], r[12], r[13], r[14], r[15], med_view, status, why or ""])
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pay_item_ids", nargs="+", type=int)
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    if not os.environ.get("SUPABASE_DB_URL"):
        raise SystemExit("SUPABASE_DB_URL is not set in .env")
    with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
        conn.execute("set session characteristics as transaction read only")
        rows = build(conn, args.pay_item_ids)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(["" if v is None else v for v in r] for r in rows)
    print(f"wrote {OUT.relative_to(ROOT)}: {len(rows)} lines")
    for pid in args.pay_item_ids:
        mine = [r for r in rows if r[0] == pid]
        counts = {s: sum(r[18] == s for r in mine) for s in ("kept", "dropped_outlier", "not_in_stats")}
        print(f"  {pid} {mine[0][1]} {mine[0][2][:45]} ({mine[0][3]}): median {mine[0][17]}, "
              + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
