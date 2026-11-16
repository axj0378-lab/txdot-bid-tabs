"""Schema checks against the Estimate Master Database (SPEC.md "Data model").

Read-only: the session is opened READ ONLY and nothing is inserted.
Skipped when SUPABASE_DB_URL is not set (e.g. offline runs).

Run:  python -m pytest -q tests/test_schema.py
"""

import os
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
dotenv = pytest.importorskip("dotenv")

dotenv.load_dotenv(Path(__file__).resolve().parents[1] / ".env")
DB_URL = os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(not DB_URL, reason="SUPABASE_DB_URL not set")

TABLES = [
    "units", "airports", "projects", "project_airports", "bidders", "bids",
    "bid_schedule_totals", "pay_items", "pay_item_aliases", "bid_line_items",
    "cost_indices", "regional_factors", "cost_settings",
    "treatment_pay_items", "estimates", "estimate_line_items",
]

VIEWS = [
    "v_unit_prices_adjusted", "v_unit_price_stats", "v_estimate_vs_bids",
    "v_bid_total_check", "v_review_queue",
]


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DB_URL) as c:
        c.execute("set session characteristics as transaction read only")
        yield c


def _relations(conn, relkind):
    rows = conn.execute(
        """select c.relname, c.relrowsecurity, c.reloptions
           from pg_class c join pg_namespace n on n.oid = c.relnamespace
           where n.nspname = 'public' and c.relkind = %s""",
        (relkind,),
    ).fetchall()
    return {name: (rls, opts or []) for name, rls, opts in rows}


def test_all_tables_exist(conn):
    missing = set(TABLES) - set(_relations(conn, "r"))
    assert not missing, f"missing tables: {sorted(missing)}"


def test_all_views_exist(conn):
    missing = set(VIEWS) - set(_relations(conn, "v"))
    assert not missing, f"missing views: {sorted(missing)}"


def test_rls_enabled_on_every_table(conn):
    # Every table in public, not only the SPEC list, so a new table without RLS fails here.
    no_rls = sorted(name for name, (rls, _) in _relations(conn, "r").items() if not rls)
    assert not no_rls, f"RLS disabled on: {no_rls}"


def test_views_are_security_invoker(conn):
    # Views must respect RLS of the underlying tables.
    views = _relations(conn, "v")
    bad = sorted(v for v in VIEWS if "security_invoker=true" not in views.get(v, (None, []))[1])
    assert not bad, f"views without security_invoker: {bad}"


def test_functions_have_fixed_search_path(conn):
    rows = conn.execute(
        """select p.proname
           from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'public'
             and not exists (select 1 from unnest(coalesce(p.proconfig, '{}')) c
                             where c like 'search_path=%')"""
    ).fetchall()
    assert not rows, f"functions without a fixed search_path: {sorted(r[0] for r in rows)}"


@pytest.mark.parametrize("view", VIEWS)
def test_view_is_queryable(conn, view):
    conn.execute(f"select * from public.{view} limit 0")
