"""Tests for the airport auto-match (ingest/match_airport.py), its use in ingest/sync_project.py,
the nightly report (scripts/nightly_check.py) and main.py --skip-existing.

Database tests only read, or run sync_project as a dry run (rolled back, CSV untouched).
"""

import csv
import os
from datetime import datetime
from pathlib import Path

import pytest

import main
from ingest import match_airport as ma
from ingest.match_airport import Airport, auto_map, match_airport, words
from txdot_bid_tabs.models import ProjectListing

ROOT = Path(__file__).resolve().parents[1]
dotenv = pytest.importorskip("dotenv")
dotenv.load_dotenv(ROOT / ".env")
DB_URL = os.environ.get("SUPABASE_DB_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="SUPABASE_DB_URL not set")

AIRPORTS = [
    Airport("50R", "Lockhart Municipal Airport", "Lockhart"),
    Airport("PRS", "Presidio Lely International Airport", "Presidio"),
    Airport("ILE", "Skylark Field Airport", "Killeen"),
    Airport("GYI", "North Texas Regional Airport/Perrin Field", "Sherman/Denison"),
    Airport("OCH", "A.L Mangham Jr. Regional Airport", "Nacogdoches"),
    Airport("E41", "Reagan County", "Big Lake"),
    Airport("IAH", "George Bush Intercontinental", "Houston"),
    Airport("HOU", "William P Hobby", "Houston"),
    Airport("CXO", "North Houston Regional", "Houston"),
    Airport("AAA", "Jackson County", "Edna"),
    Airport("BBB", "Jackson County", "Somewhere"),
]


def faa(name):
    return match_airport(name, AIRPORTS).faa_id


# ---------------------------------------------------------------- matching rules

def test_words_normalizes():
    assert words("A.L. Mangham Jr. Regional Airport") == ["AL", "MANGHAM", "JR", "REGIONAL"]
    assert words("Reagan Co. Muni Arpt") == ["REAGAN", "COUNTY", "MUNICIPAL", "ARPT"]
    assert words("Scholes Int'l Airport at Galveston") == ["SCHOLES", "INTERNATIONAL", "GALVESTON"]


@pytest.mark.parametrize("name,expected,rule", [
    ("Skylark Field (ILE)", "ILE", "FAA id in name"),
    ("Lockhart Municipal Airport", "50R", "same name"),
    ("LOCKHART MUNI", "50R", "same name"),
    ("A.L. Mangham Jr. Regional Airport", "OCH", "same name"),
    ("Reagan County Airport", "E41", "same name"),
    ("Presidio International Airport", "PRS", "name contains"),
    ("North Texas Regional Airport", "GYI", "name contains"),
    ("Conroe-North Houston Regional Airport", "CXO", "name contains"),
    ("Big Lake Airport Improvements", "E41", "city"),
    ("Denison Runway Rehab", "GYI", "city"),
])
def test_confident_match(name, expected, rule):
    m = match_airport(name, AIRPORTS)
    assert (m.faa_id, m.rule) == (expected, rule)


@pytest.mark.parametrize("name,why", [
    ("Jackson County Airport", "same name fits 2 airports"),        # two airports, same name
    ("Houston Executive Airport", "city fits 3 airports"),           # city shared by 3
    ("Regional Airport", "no airport name or city"),                 # one word can't 'contain'
    ("Gold Coast Spaceport", "no airport name or city"),
    ("", "no airport name"),
    ("Statewide Group Juliet: Brooks County (Falfurrias), Mid Valley (Weslaco)", "multi-airport"),
    ("Jones Field; Commerce Municipal", "multi-airport"),
])
def test_no_match_unless_exactly_one(name, why):
    m = match_airport(name, AIRPORTS)
    assert m.faa_id is None and why in m.reason


def test_unknown_id_in_parentheses_is_ignored():
    assert faa("Lockhart Municipal (Phase 2)") == "50R"
    assert faa("Skylark Field (XYZ)") == "ILE"              # falls through to the name rules


def test_id_in_name_beats_name_rules():
    assert faa("North Houston Regional (IAH)") == "IAH"


# ---------------------------------------------------------------- CSV

def _write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ma.CSV_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def test_auto_map_appends_source_auto(tmp_path):
    path = tmp_path / "project_airports.csv"
    _write(path, [{"project_id": "OLD1", "faa_id": "E41", "note": "", "source": "manual"}])
    added, unresolved = auto_map({"OLD1": "Reagan County", "NEW1": "Lockhart Municipal Airport",
                                  "NEW2": "Jackson County Airport"}, AIRPORTS, {"OLD1"}, path=path)
    assert added == [("NEW1", "50R", "same name")]
    assert [u[0] for u in unresolved] == ["NEW2"]
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert rows[0] == {"project_id": "OLD1", "faa_id": "E41", "note": "", "source": "manual"}
    assert rows[1] == {"project_id": "NEW1", "faa_id": "50R", "note": "auto: same name", "source": "auto"}
    assert len(rows) == 2


def test_auto_map_write_false_leaves_csv(tmp_path):
    path = tmp_path / "project_airports.csv"
    _write(path, [])
    before = path.read_bytes()
    added, _ = auto_map({"NEW1": "Lockhart Municipal Airport"}, AIRPORTS, set(), path=path, write=False)
    assert added and path.read_bytes() == before


def test_project_airports_csv_has_source_column():
    rows = ma.read_mapping()
    assert list(rows[0]) == ma.CSV_COLUMNS
    assert {r["source"] for r in rows} <= {"manual", "auto"}


# ---------------------------------------------------------------- against the real airports table

@pytest.fixture(scope="module")
def real_airports():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DB_URL, connect_timeout=30) as c:
        return ma.read_airports(c)


@needs_db
def test_every_airport_matches_itself(real_airports):
    wrong = [(a.faa_id, m.faa_id) for a in real_airports
             if (m := match_airport(a.name, real_airports)).faa_id not in (a.faa_id, None)]
    assert not wrong


@needs_db
def test_workbook_projects_never_matched_wrong(real_airports):
    """Every workbook project, as if it were new: the match is the hand-made one or none."""
    from openpyxl import load_workbook

    from ingest.load_master import WORKBOOK
    from ingest.sync_project import _workbook_airport_names
    names = _workbook_airport_names(load_workbook(WORKBOOK, read_only=True))
    truth = {}
    for r in ma.read_mapping():
        truth.setdefault(r["project_id"], set()).add(r["faa_id"])
    results = {pid: match_airport(name, real_airports) for pid, name in names.items()}
    assert not [pid for pid, m in results.items() if m.faa_id and truth[pid] != {m.faa_id}]
    single = [pid for pid in names if len(truth[pid]) == 1]
    assert all(results[pid].faa_id for pid in single)          # all 25 single-airport projects
    assert not [pid for pid in names if len(truth[pid]) > 1 and results[pid].faa_id]


@needs_db
def test_sync_auto_maps_missing_project_dry_run(monkeypatch):
    """Lockhart, pretending it isn't in the CSV: matched to 50R, loaded (no changes), CSV untouched."""
    from ingest.sync_project import sync_project
    monkeypatch.setattr(ma, "mapped_projects", lambda path=None: set())
    before = ma.PROJECT_AIRPORTS_CSV.read_bytes()
    s = sync_project("NTB2614LHART00034", dry_run=True, verbose=False)
    assert s["airport_added"] == ("50R", "same name")
    assert sum(s["changes"].values()) == 0
    assert ma.PROJECT_AIRPORTS_CSV.read_bytes() == before


@needs_db
def test_sync_unmatched_airport_stops_with_fix(monkeypatch):
    import ingest.sync_project as sp
    monkeypatch.setattr(ma, "mapped_projects", lambda path=None: set())
    monkeypatch.setattr(sp, "_workbook_airport_names", lambda wb: {"NTB2614LHART00034": "Gold Coast Spaceport"})
    with pytest.raises(sp.SyncError, match=r"not matched.*project_airports\.csv.*sync_project\.py NTB2614LHART00034"):
        sp.sync_project("NTB2614LHART00034", dry_run=True, verbose=False)


def test_sync_bad_db_url_hides_details(tmp_path):
    from ingest.sync_project import SyncError, sync_project
    url = "postgresql://someuser:secretpw@127.0.0.1:1/nodb"
    with pytest.raises(SyncError) as e:
        sync_project("NTB2614LHART00034", dry_run=True, verbose=False, db_url=url)
    assert "secretpw" not in str(e.value) and "someuser" not in str(e.value)
    assert "check SUPABASE_DB_URL" in str(e.value)


# ---------------------------------------------------------------- nightly report

def test_report_severity():
    from scripts.nightly_check import build_report
    assert build_report(0, [], [], [], {}, [])[1] == "ok"
    assert build_report(0, [], [], [], {"P1": 3}, [])[1] == "warn"
    assert build_report(2, [], [], [], {}, [])[1] == "fail"
    md, sev = build_report(0, [], [("P2", "airport not matched")], [("P3", "50R", "auto: same name")],
                           {"P1": 3}, [])
    assert sev == "fail"
    assert "`P2`: airport not matched" in md and "`P3` -> `50R`" in md and "3 lines in v_review_queue" in md


def test_log_lines_are_sanitized():
    from scripts.nightly_check import log_problems
    log = ('10:00:00 INFO    fine\n'
           '10:00:01 ERROR   database not updated: connection to server at '
           '"aws-0-us-east-1.pooler.supabase.com" (52.1.2.3), port 5432 failed: '
           'FATAL: password authentication failed for user "postgres.abcdef"\n'
           '10:00:02 ERROR   url postgresql://u:p@host:5432/db\n')
    lines = log_problems(log)
    assert len(lines) == 2
    text = "\n".join(lines)
    for leak in ("pooler.supabase.com", "52.1.2.3", "postgres.abcdef", "u:p@host"):
        assert leak not in text


# ---------------------------------------------------------------- main.py --skip-existing

def test_skip_existing_processes_only_new(monkeypatch):
    when = datetime(2026, 11, 1, 10)
    listings = [ProjectListing("Lockhart", "NTB2614LHART00034", when, "http://x/a.pdf"),
                ProjectListing("New One", "NTB9999NEWAP00001", when, "http://x/b.pdf")]
    done = []
    monkeypatch.setattr(main.scraper, "list_projects", lambda **k: listings)
    monkeypatch.setattr(main.scraper, "download_pdf", lambda l, force: Path(l.pdf_url))
    monkeypatch.setattr(main, "process", lambda pdf, l, sync_db=True: done.append(l.project_id) or True)
    monkeypatch.setattr("sys.argv", ["main.py", "--all-new", "--skip-existing"])
    assert main.main() == 0
    assert done == ["NTB9999NEWAP00001"]

    done.clear()
    listings.pop()
    assert main.main() == 0 and done == []                   # nothing new is not an error
