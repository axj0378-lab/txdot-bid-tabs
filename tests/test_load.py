"""Acceptance tests for ingest/load_master.py (SPEC.md), the ones that don't need pay items.

Database tests are skipped when SUPABASE_DB_URL is not set. All are read-only except
test_rerun_changes_nothing, which re-runs the loader inside a transaction that is always
rolled back (about 40 s).
"""

import os
from collections import defaultdict
from pathlib import Path

import pytest

from ingest.load_master import (ESTIMATE_BIDDER, WORKBOOK, bidder_key, build_projects, normalize_bidder,
                                read_merges, read_sheet)

psycopg = pytest.importorskip("psycopg")
dotenv = pytest.importorskip("dotenv")
openpyxl = pytest.importorskip("openpyxl")

ROOT = Path(__file__).resolve().parents[1]
dotenv.load_dotenv(ROOT / ".env")
DB_URL = os.environ.get("SUPABASE_DB_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="SUPABASE_DB_URL not set")


# ---------------------------------------------------------------- no database

@pytest.mark.parametrize("raw, expected", [
    ("Don Jackson Construction, Inc.", "DON JACKSON CONSTRUCTION"),
    ("Don Jackson Construction", "DON JACKSON CONSTRUCTION"),
    ("Allen Keller Company, LLC", "ALLEN KELLER"),
    ("Anderson Columbia Company, Inc.", "ANDERSON COLUMBIA"),
    ("Zachry Construction Corporation", "ZACHRY CONSTRUCTION"),
    ("EAS Contracting, L.P.", "EAS CONTRACTING"),
    ("KOBO Utility Construction Corp", "KOBO UTILITY CONSTRUCTION"),
    ("H.V. Carver, Inc.", "HV CARVER"),
    ("F&W Electrical", "F W ELECTRICAL"),
    ("Knife River Corporation-South", "KNIFE RIVER SOUTH"),
])
def test_normalize_bidder(raw, expected):
    assert normalize_bidder(raw) == expected


def test_merges_and_estimates():
    merges = read_merges()
    assert bidder_key("Lone Wolf, LLC", False, merges) == "LONE WOLF CONSTRUCTION"
    assert bidder_key("Knife River Corporation-South", False, merges) == "KNIFE RIVER"
    assert bidder_key("GDS, LLC dba Ambrozi Contracting LLC", False, merges) == "AMBROZI CONTRACTING"
    assert bidder_key("Parkhill (Engineer's Estimate)", True, merges) == ESTIMATE_BIDDER


# ---------------------------------------------------------------- database

@pytest.fixture(scope="module")
def workbook():
    wb = openpyxl.load_workbook(WORKBOOK, read_only=True, data_only=True)
    return {"wb": wb, "bid_rows": read_sheet(wb, "Bid Data"), "projects": read_sheet(wb, "Projects")}


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DB_URL) as c:
        c.execute("set session characteristics as transaction read only")
        yield c


def _one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


@needs_db
def test_row_counts_match_workbook(conn, workbook):
    assert _one(conn, "select count(*) from public.projects") == len(workbook["projects"]) == 28
    assert _one(conn, "select count(*) from public.bid_line_items") == len(workbook["bid_rows"])


@needs_db
def test_printed_schedule_totals_reconcile(conn):
    rows = conn.execute(
        "select project_id, bidder, schedule, difference from public.v_bid_total_check where not is_estimate"
    ).fetchall()
    assert rows, "no printed totals loaded"
    bad = [r for r in rows if r[3] != 0]
    assert not bad, bad


@needs_db
def test_award_totals_equal_award_schedule_lines(conn):
    # Covers projects with several award schedules (2512BAYCY, 4224AVSAMK), which have no per-schedule total.
    bad = conn.execute("""
        select p.project_id, bd.name, b.award_total, sum(li.extended_price)
        from public.bids b
        join public.projects p on p.id = b.project_id
        join public.bidders bd on bd.id = b.bidder_id
        left join public.bid_line_items li on li.bid_id = b.id and li.schedule = any (p.award_schedules)
        where not b.is_estimate
        group by p.project_id, bd.name, b.award_total
        having b.award_total is distinct from sum(li.extended_price)""").fetchall()
    assert not bad, bad
    n = _one(conn, """select count(*) from public.bids b join public.projects p on p.id = b.project_id
                      where not b.is_estimate and p.source = 'legacy tab'""")
    assert n == 100   # SPEC: 100 legacy bidder totals


@needs_db
def test_low_bidders_match_workbook(conn, workbook):
    merges = read_merges()
    expected = {r["Project ID"]: bidder_key(r["Bidder"], False, merges)
                for r in workbook["bid_rows"] if r["Low Bid"] == "Yes"}
    got = dict(conn.execute("""
        select p.project_id, bd.name_normalized from public.bids b
        join public.projects p on p.id = b.project_id join public.bidders bd on bd.id = b.bidder_id
        where b.is_low""").fetchall())
    assert len(expected) == 28
    assert got == expected
    assert got["2509TEMPL"] == "DON JACKSON CONSTRUCTION"
    assert got["2512GLVST"] == "WEBBER"
    assert got["STWJULIET"] == "ALLEN KELLER"
    assert got["NTB2614LHART00034"] == "DELTA SPECIALTY CONTRACTORS"


@needs_db
def test_engineers_estimates(conn):
    rows = conn.execute("""
        select b.is_estimate, b.rank, b.is_low, bd.name_normalized, b.bidder_name_raw
        from public.bids b join public.bidders bd on bd.id = b.bidder_id
        where b.is_estimate or bd.name_normalized = %s""", (ESTIMATE_BIDDER,)).fetchall()
    assert len(rows) == 19
    assert all(est and rank is None and not low and key == ESTIMATE_BIDDER for est, rank, low, key, _ in rows)
    assert "Parkhill (Engineer's Estimate)" in {r[4] for r in rows}


@needs_db
def test_every_project_resolves_to_airports(conn):
    assert _one(conn, """select count(*) from public.projects p
                         where not exists (select 1 from public.project_airports x where x.project_id = p.id)""") == 0


@needs_db
def test_rerun_changes_nothing(workbook):
    from ingest.load_master import load
    projects, bidder_names, _ = build_projects(workbook["wb"])
    with psycopg.connect(DB_URL, autocommit=True) as c:
        with c.transaction(force_rollback=True):
            changes = load(c, projects, bidder_names, verbose=False)
    assert sum(changes.values()) == 0, {k: v for k, v in changes.items() if v}
