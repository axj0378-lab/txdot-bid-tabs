"""Export v_review_queue to data/review/queue.csv for hand review (PLAYBOOK Session 5).

One row per line item in the queue, with what you need to check it against the bid-tab PDF
(project, bidder, schedule, raw code/description/unit, quantity, unit price) and the current
match. Fill in, per row:

  confirmed_pay_item_id  the right pay item id (copy current_pay_item_id to confirm it);
                         ids are listed in data/review/pay_items.csv
  exclude                Y = keep the line in the bid but leave it out of price statistics
                         (OCR fragments, garbled descriptions)
  reviewer_note          optional; stored in bid_line_items.review_notes

Rows left blank are not touched. Then run scripts/apply_review.py.

Also writes data/review/pay_items.csv (every pay item with its id and line count).
Read-only against the database. Refuses to overwrite a queue.csv that already holds decisions
unless --force.

Run:  python scripts/export_review_queue.py [--force]
"""

import argparse
import csv
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
QUEUE_CSV = ROOT / "data" / "review" / "queue.csv"
PAY_ITEMS_CSV = ROOT / "data" / "review" / "pay_items.csv"
DECISION_COLUMNS = ["confirmed_pay_item_id", "exclude", "reviewer_note"]

QUEUE_SQL = """
    select q.line_item_id, q.project_id, q.bidder, b.is_estimate, q.schedule, q.line_no,
           q.raw_item_code, q.raw_description, q.raw_unit, li.unit_code, q.quantity, q.unit_price,
           li.extended_price, q.match_method, q.match_score,
           q.pay_item_id, pi.spec_section, pi.canonical_name, q.review_notes
    from public.v_review_queue q
    join public.bid_line_items li on li.id = q.line_item_id
    join public.bids b on b.id = li.bid_id
    left join public.pay_items pi on pi.id = q.pay_item_id
    order by pi.spec_section nulls last, pi.canonical_name, q.project_id, q.schedule, q.line_no, q.bidder"""
QUEUE_COLUMNS = ["line_item_id", "project_id", "bidder", "is_estimate", "schedule", "line_no",
                 "raw_item_code", "raw_description", "raw_unit", "unit_code", "quantity", "unit_price",
                 "extended_price", "match_method", "match_score",
                 "current_pay_item_id", "current_spec_section", "current_pay_item_name", "review_notes",
                 *DECISION_COLUMNS]

PAY_ITEMS_SQL = """
    select pi.id, pi.spec_section, pi.canonical_name, pi.unit_code, pi.category, count(li.id)
    from public.pay_items pi left join public.bid_line_items li on li.pay_item_id = pi.id
    group by pi.id order by pi.spec_section, pi.canonical_name, pi.unit_code"""
PAY_ITEMS_COLUMNS = ["pay_item_id", "spec_section", "canonical_name", "unit_code", "category", "n_lines"]


def has_decisions(path):
    if not path.exists():
        return False
    with open(path, newline="", encoding="utf-8-sig") as f:
        return any((r.get(c) or "").strip() for r in csv.DictReader(f) for c in DECISION_COLUMNS)


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:    # BOM so Excel reads UTF-8
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow(["" if v is None else ("Y" if v is True else "" if v is False else v) for v in r])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--force", action="store_true", help="overwrite a queue.csv that already holds decisions")
    args = ap.parse_args()
    if has_decisions(QUEUE_CSV) and not args.force:
        raise SystemExit(f"{QUEUE_CSV.relative_to(ROOT)} already holds decisions; apply it first "
                         "(scripts/apply_review.py) or pass --force to overwrite")

    load_dotenv(ROOT / ".env")
    if not os.environ.get("SUPABASE_DB_URL"):
        raise SystemExit("SUPABASE_DB_URL is not set in .env")
    with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
        conn.execute("set session characteristics as transaction read only")
        queue = [(*r, "", "", "") for r in conn.execute(QUEUE_SQL)]
        pay_items = conn.execute(PAY_ITEMS_SQL).fetchall()

    write_csv(QUEUE_CSV, QUEUE_COLUMNS, queue)
    write_csv(PAY_ITEMS_CSV, PAY_ITEMS_COLUMNS, pay_items)
    groups = {(r[16], r[17]) for r in queue}
    print(f"wrote {QUEUE_CSV.relative_to(ROOT)}: {len(queue)} lines in {len(groups)} pay items, "
          f"{len({r[1] for r in queue})} projects")
    print(f"wrote {PAY_ITEMS_CSV.relative_to(ROOT)}: {len(pay_items)} pay items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
