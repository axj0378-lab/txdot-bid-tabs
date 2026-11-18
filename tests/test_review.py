"""Tests for the review queue round trip (scripts/export_review_queue.py, scripts/apply_review.py).

The database test applies decisions inside a transaction that is always rolled back.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from apply_review import Decision, apply, parse_decisions  # noqa: E402

psycopg = pytest.importorskip("psycopg")
dotenv = pytest.importorskip("dotenv")
dotenv.load_dotenv(ROOT / ".env")
DB_URL = os.environ.get("SUPABASE_DB_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="SUPABASE_DB_URL not set")

LINES = {1: ("LF", 10), 2: ("EA", 20), 3: ("LF", 10)}
PAY_ITEM_UNITS = {10: "LF", 11: "LF", 20: "EA"}


def row(lid, pid="", exclude="", note=""):
    return {"line_item_id": str(lid), "confirmed_pay_item_id": pid, "exclude": exclude, "reviewer_note": note}


# ---------------------------------------------------------------- no database

def test_parse_confirm_exclude_and_blank():
    decisions, errors = parse_decisions(
        [row(1, "11"), row(2, exclude="y", note="OCR fragment"), row(3)], LINES, PAY_ITEM_UNITS)
    assert errors == []
    assert decisions == [Decision(1, 11, False, None),
                         Decision(2, 20, True, "OCR fragment")]     # exclude keeps the current pay item


def test_parse_excel_float_id_and_exclude_with_new_item():
    decisions, errors = parse_decisions([row(1, "11.0", "Y")], LINES, PAY_ITEM_UNITS)
    assert errors == [] and decisions == [Decision(1, 11, True, None)]


@pytest.mark.parametrize("bad, fragment", [
    (row(1, "20"), "pay item 20 is EA, line is LF"),
    (row(1, "99"), "pay item 99 not found"),
    (row(9, "10"), "line_item_id 9 not found"),
    (row(1, "abc"), "confirmed_pay_item_id 'abc'"),
    (row(1, exclude="maybe"), "exclude 'maybe'"),
    (row(1, note="looks fine"), "reviewer_note without a decision"),
])
def test_parse_rejects(bad, fragment):
    decisions, errors = parse_decisions([bad], LINES, PAY_ITEM_UNITS)
    assert decisions == [] and len(errors) == 1 and fragment in errors[0]


def test_parse_rejects_duplicate_line():
    _, errors = parse_decisions([row(1, "10"), row(1, "11")], LINES, PAY_ITEM_UNITS)
    assert len(errors) == 1 and "appears twice" in errors[0]


# ---------------------------------------------------------------- database

@needs_db
def test_apply_round_trip():
    from ingest.load_catalog import load, read_catalog
    with psycopg.connect(DB_URL, autocommit=True) as c:
        with c.transaction(force_rollback=True):
            # put two alias-matched contractor lines into the queue (the real queue may be empty)
            queue = c.execute("""select li.id, li.pay_item_id from public.bid_line_items li
                                 join public.bids b on b.id = li.bid_id
                                 where li.match_method = 'alias' and not b.is_estimate and li.quantity > 0
                                   and not li.excluded_from_stats
                                 order by li.id limit 2""").fetchall()
            assert len(queue) == 2
            (keep_id, keep_pid), (excl_id, _) = queue
            c.execute("update public.bid_line_items set needs_review = true where id = any(%s)",
                      ([keep_id, excl_id],))
            before = c.execute("select count(*) from public.v_review_queue").fetchone()[0]

            # pretend there is no alias yet for the confirmed line, so one gets added
            c.execute("""delete from public.pay_item_aliases a using public.bid_line_items li
                         where li.id = %s and a.pay_item_id = li.pay_item_id""", (keep_id,))
            decisions = [Decision(keep_id, keep_pid, False, "checked against PDF"),
                         Decision(excl_id, None, True, "OCR fragment")]
            changes = apply(c, decisions, verbose=False)
            assert changes["confirmed"] == 1 and changes["excluded"] == 1
            assert changes["pay_item_aliases.inserted"] >= 1

            after = c.execute("select count(*) from public.v_review_queue").fetchone()[0]
            assert after == before - 2
            m, review, excl, note = c.execute("""select match_method, needs_review, excluded_from_stats, review_notes
                                                 from public.bid_line_items where id = %s""", (keep_id,)).fetchone()
            assert (m, review, excl, note) == ("manual", False, False, "checked against PDF")
            assert c.execute("select excluded_from_stats from public.bid_line_items where id = %s",
                             (excl_id,)).fetchone()[0] is True
            assert c.execute("select count(*) from public.v_unit_prices_adjusted where line_item_id = %s",
                             (excl_id,)).fetchone()[0] == 0

            # re-applying changes nothing; the catalog loader leaves manual lines alone
            again = apply(c, decisions, verbose=False)
            assert again["bid_line_items.updated"] == 0 and again["pay_item_aliases.inserted"] == 0
            load(c, *read_catalog(), verbose=False)
            assert c.execute("""select count(*) from public.bid_line_items
                                where id = any(%s) and match_method = 'manual' and not needs_review""",
                             ([keep_id, excl_id],)).fetchone()[0] == 2
