"""Airports mirror checks (PLAYBOOK Session 2), after scripts/sync_airports.py.

Read-only against the Estimate Master Database; skipped when SUPABASE_DB_URL is not set.
"""

import csv
import os
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
dotenv = pytest.importorskip("dotenv")

ROOT = Path(__file__).resolve().parents[1]
dotenv.load_dotenv(ROOT / ".env")
DB_URL = os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(not DB_URL, reason="SUPABASE_DB_URL not set")


def _csv(name):
    with open(ROOT / "data" / name, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def airports():
    with psycopg.connect(DB_URL) as c:
        c.execute("set session characteristics as transaction read only")
        rows = c.execute("select faa_id, txdot_district, region, source from public.airports").fetchall()
    return {faa: {"district": d, "region": r, "source": s} for faa, d, r, s in rows}


def test_mirror_is_loaded(airports):
    assert len(airports) >= 246


def test_every_project_airport_resolves_with_a_region(airports):
    used = {r["faa_id"].strip().upper() for r in _csv("project_airports.csv")}
    missing = sorted(used - set(airports))
    no_region = sorted(f for f in used & set(airports) if not airports[f]["region"])
    assert not missing, f"project airports missing from airports: {missing}"
    assert not no_region, f"project airports without a region: {no_region}"


def test_overrides_applied(airports):
    for o in _csv("airport_overrides.csv"):
        a = airports.get(o["faa_id"].upper())
        assert a is not None, o["faa_id"]
        assert a["source"] == o["source"], o["faa_id"]
        if not a["district"] or a["district"] == o["txdot_district"]:
            assert a["region"] == (o["region"] or o["txdot_district"]), o["faa_id"]
