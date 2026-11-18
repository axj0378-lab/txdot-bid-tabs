"""Tests for scripts/load_cost_index.py.

The database test loads and activates a temporary index inside a transaction that is always
rolled back, then checks from a fresh connection that nothing was left behind (no index rows,
no active index: prices stay nominal while the index choice is DEFERRED).
"""

import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from load_cost_index import INDICES_DIR, load_index, read_index_csv, resolve  # noqa: E402

psycopg = pytest.importorskip("psycopg")
dotenv = pytest.importorskip("dotenv")
dotenv.load_dotenv(ROOT / ".env")
DB_URL = os.environ.get("SUPABASE_DB_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="SUPABASE_DB_URL not set")

TEMP_INDEX = "PYTEST_TEMP_INDEX"


def write(tmp_path, text, name="idx.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------- no database

def test_read_index_csv(tmp_path):
    p = write(tmp_path, "date,value\n2024-04-01,103.2\n2024-01,100\n\n2024-07-01,\"1,104.5\"\n")
    assert read_index_csv(p) == [(date(2024, 1, 1), Decimal("100")),
                                 (date(2024, 4, 1), Decimal("103.2")),
                                 (date(2024, 7, 1), Decimal("1104.5"))]


def test_read_index_csv_header_case_and_bom(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_text("﻿Date,Value\n2025-01-01,1\n", encoding="utf-8")
    assert read_index_csv(p) == [(date(2025, 1, 1), Decimal("1"))]


@pytest.mark.parametrize("body, fragment", [
    ("2024-13-01,100", "date '2024-13-01'"),
    ("01/04/2024,100", "date '01/04/2024'"),
    ("2024-01-01,0", "value '0'"),
    ("2024-01-01,-5", "value '-5'"),
    ("2024-01-01,abc", "value 'abc'"),
    ("2024-01-01,100\n2024-01,101", "appears twice"),
])
def test_read_index_csv_rejects(tmp_path, body, fragment):
    with pytest.raises(ValueError) as e:
        read_index_csv(write(tmp_path, "date,value\n" + body + "\n"))
    assert fragment in str(e.value)


def test_read_index_csv_needs_columns_and_rows(tmp_path):
    with pytest.raises(ValueError, match="columns must be date, value"):
        read_index_csv(write(tmp_path, "period,index\n2024-01-01,100\n"))
    with pytest.raises(ValueError, match="no rows"):
        read_index_csv(write(tmp_path, "date,value\n"))


def test_bare_name_resolves_to_indices_dir():
    assert resolve("nhcci.csv") == INDICES_DIR / "nhcci.csv"
    assert resolve("some/dir/x.csv") == Path("some/dir/x.csv")


# ---------------------------------------------------------------- database

def _state(conn):
    return (conn.execute("select count(*) from public.cost_indices where index_name = %s", (TEMP_INDEX,)).fetchone()[0],
            conn.execute("select active_index_name from public.cost_settings where id").fetchone()[0])


@needs_db
def test_load_and_activate_temporary_index_rolled_back(tmp_path):
    # 100 before every bid date, 110 after: adjusted price = 1.10 x unit price
    rows = read_index_csv(write(tmp_path, "date,value\n1900-01-01,100\n2100-01-01,110\n"))
    with psycopg.connect(DB_URL, autocommit=True) as c:
        before_active = _state(c)[1]
        with c.transaction(force_rollback=True):
            changes = load_index(c, TEMP_INDEX, rows, source="pytest")
            assert (changes["inserted"], changes["updated"], changes.get("activated", 0)) == (2, 0, 0)
            assert _state(c) == (2, before_active)                     # loaded, not activated

            again = load_index(c, TEMP_INDEX, rows, source="pytest", activate=True)
            assert (again["inserted"], again["updated"], again["activated"]) == (0, 0, 1)
            assert _state(c) == (2, TEMP_INDEX)
            n, bad = c.execute("""
                select count(*), count(*) filter (where adjusted_unit_price is distinct from round(unit_price * 1.10, 4))
                from public.v_unit_prices_adjusted""").fetchone()
            assert n > 0 and bad == 0

            # a CSV that drops a period keeps the stored one and reports it
            partial = load_index(c, TEMP_INDEX, rows[:1], source="pytest")
            assert partial["not_in_csv"] == 1 and _state(c)[0] == 2

    with psycopg.connect(DB_URL) as c:                                 # nothing left behind
        assert _state(c) == (0, before_active)


@needs_db
def test_no_index_is_active():
    """SPEC.md: inflation index DEFERRED, prices nominal."""
    with psycopg.connect(DB_URL) as c:
        assert c.execute("select active_index_name from public.cost_settings where id").fetchone()[0] is None
        assert c.execute("""select count(*) from public.v_unit_prices_adjusted
                            where adjusted_unit_price is distinct from unit_price""").fetchone()[0] == 0
