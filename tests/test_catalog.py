"""Acceptance tests for the pay-item catalog (ingest/load_catalog.py) and the price views (SPEC.md).

Database tests are skipped when SUPABASE_DB_URL is not set. All are read-only except the ones
that write inside a transaction that is always rolled back (index test, re-run test).
"""

import os
from pathlib import Path

import pytest

from ingest.load_catalog import read_catalog

psycopg = pytest.importorskip("psycopg")
dotenv = pytest.importorskip("dotenv")

ROOT = Path(__file__).resolve().parents[1]
dotenv.load_dotenv(ROOT / ".env")
DB_URL = os.environ.get("SUPABASE_DB_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="SUPABASE_DB_URL not set")


# ---------------------------------------------------------------- no database

def test_reviewed_catalog_is_valid():
    pay_items, aliases, check_notes = read_catalog()
    assert len(aliases) == 977
    assert len(pay_items) == 462
    assert all(v["category"] for v in pay_items.values())
    assert set(aliases.values()) == set(pay_items)
    assert set(check_notes) <= set(aliases)


def test_invalid_rows_are_rejected(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "spec_section,description_clean,unit,pay_item_spec_section,pay_item_name,category,review_note\n"
        "P-401,ASPHALT,TON,,ASPHALT,paving,\n"            # blank section
        "P-401,ASPHALT 2,XX,P-401,ASPHALT,paving,\n"      # unknown unit
        "P-401,ASPHALT 3,TON,P-401,ASPHALT,paving,\n"
        "P-401,ASPHALT 4,TON,P-401,ASPHALT,marking,\n",   # category conflict
        encoding="utf-8")
    with pytest.raises(ValueError) as e:
        read_catalog(bad, units={"TON"})
    msg = str(e.value)
    assert "3 invalid rows" in msg and "row 2" in msg and "row 3" in msg and "row 5" in msg


# ---------------------------------------------------------------- database

@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DB_URL) as c:
        c.execute("set session characteristics as transaction read only")
        yield c


def _one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


@needs_db
def test_every_line_item_is_matched(conn):
    assert _one(conn, "select count(*) from public.bid_line_items where pay_item_id is null") == 0
    assert _one(conn, """select count(*) from public.bid_line_items li join public.pay_items pi on pi.id = li.pay_item_id
                         where li.unit_code is distinct from pi.unit_code""") == 0
    # every contractor line was reviewed by hand in the catalog CSV
    assert _one(conn, """select count(*) from public.bid_line_items li join public.bids b on b.id = li.bid_id
                         where not b.is_estimate and li.match_method not in ('alias', 'manual')""") == 0
    assert _one(conn, "select count(*) from public.v_review_queue") == \
        _one(conn, "select count(*) from public.bid_line_items where needs_review")


@needs_db
def test_no_estimate_in_adjusted_prices(conn):
    assert _one(conn, "select count(*) from public.v_unit_prices_adjusted") > 0
    assert _one(conn, """select count(*) from public.v_unit_prices_adjusted v join public.bids b on b.id = v.bid_id
                         where b.is_estimate""") == 0


@needs_db
def test_stats_exclude_lump_sum_and_credits(conn):
    # both kinds exist among matched lines, so the test is not trivially true
    assert _one(conn, """select count(*) from public.bid_line_items li join public.units u on u.code = li.unit_code
                         where li.pay_item_id is not null and u.is_lump_sum""") > 0
    assert _one(conn, "select count(*) from public.bid_line_items where pay_item_id is not null and quantity < 0") > 0

    assert _one(conn, "select count(*) from public.v_unit_price_stats") > 0
    assert _one(conn, """select count(*) from public.v_unit_price_stats s
                         join public.pay_items pi on pi.id = s.pay_item_id
                         join public.units u on u.code = pi.unit_code where u.is_lump_sum""") == 0
    assert _one(conn, "select count(*) from public.v_unit_prices_adjusted where quantity <= 0") == 0
    assert _one(conn, "select count(*) from public.v_unit_price_stats where min_price <= 0") == 0


@needs_db
def test_index_adjustment():
    """2-row test index (100 at bid date, 110 now): adjusted = 1.10 x unit price."""
    with psycopg.connect(DB_URL, autocommit=True) as c:
        with c.transaction(force_rollback=True):
            assert _one(c, "select count(*) from public.projects where bid_date is null") == 0
            c.execute("""insert into public.cost_indices (index_name, period, value, source) values
                         ('TEST', '1900-01-01', 100, 'test'), ('TEST', '2100-01-01', 110, 'test')""")
            c.execute("update public.cost_settings set active_index_name = 'TEST'")
            n, bad = c.execute("""
                select count(*), count(*) filter (where adjusted_unit_price is distinct from round(unit_price * 1.10, 4))
                from public.v_unit_prices_adjusted""").fetchone()
    assert n > 0 and bad == 0


@needs_db
def test_rerun_changes_nothing():
    from ingest.load_catalog import load
    pay_items, aliases, check_notes = read_catalog()
    with psycopg.connect(DB_URL, autocommit=True) as c:
        with c.transaction(force_rollback=True):
            changes = load(c, pay_items, aliases, check_notes, verbose=False)
    assert sum(changes.values()) == 0, {k: v for k, v in changes.items() if v}


# ---------------------------------------------------------------- price lookup (Session 6)

@needs_db
def test_price_lookup_one_row_per_item_and_region(conn):
    n_items = _one(conn, "select count(*) from public.v_unit_price_stats where region = 'STATEWIDE'")
    n_regions = _one(conn, """select count(*) from (select region from public.airports where region is not null
                              union select region from public.v_unit_price_stats) r""")
    assert _one(conn, "select count(*) from public.v_unit_price_lookup") == n_items * n_regions
    assert _one(conn, """select count(*) from (select 1 from public.v_unit_price_lookup
                         group by pay_item_id, region having count(*) > 1) d""") == 0


@needs_db
def test_price_lookup_uses_region_only_with_enough_projects(conn):
    k = _one(conn, "select min_projects_region from public.cost_settings where id")
    assert k == 3
    bad = conn.execute("""
        select l.pay_item_id, l.region from public.v_unit_price_lookup l
        left join public.v_unit_price_stats r on r.pay_item_id = l.pay_item_id and r.region = l.region
             and l.region <> 'STATEWIDE'
        join public.v_unit_price_stats s on s.pay_item_id = l.pay_item_id and s.region = 'STATEWIDE'
        where l.price_source <> case when coalesce(r.n_projects, 0) >= %s then 'region' else 'statewide' end
           or l.unit_price is distinct from case when l.price_source = 'region' then r.median else s.median end
           or l.n_projects is distinct from case when l.price_source = 'region' then r.n_projects else s.n_projects end
           or l.n_bidders  is distinct from case when l.price_source = 'region' then r.n_bidders  else s.n_bidders  end
        """, (k,)).fetchall()
    assert not bad, bad[:10]


@needs_db
def test_price_lookup_follows_setting():
    with psycopg.connect(DB_URL, autocommit=True) as c:
        with c.transaction(force_rollback=True):
            at3 = _one(c, "select count(*) from public.v_unit_price_lookup where price_source = 'region'")
            c.execute("update public.cost_settings set min_projects_region = 1")
            at1 = _one(c, "select count(*) from public.v_unit_price_lookup where price_source = 'region'")
            regional = _one(c, "select count(*) from public.v_unit_price_stats where region <> 'STATEWIDE'")
    assert at1 == regional > at3


@needs_db
def test_spot_check_reproduces_view_medians(conn):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from spot_check import build
    rows = build(conn, [827, 564, 682, 831, 763])          # raises if kept lines != view n / median
    assert {r[18] for r in rows} <= {"kept", "dropped_outlier", "not_in_stats"}
