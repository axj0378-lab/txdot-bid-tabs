"""Tests for the workbook -> database hook (main.py, ingest/sync_project.py; PLAYBOOK Session 7).

main.py tests replace the extractor and the workbook writers with fakes, so no PDF is read and
no output file is written. Database tests run in transactions that are rolled back.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
from ingest.sync_project import SyncError, sync_project

psycopg = pytest.importorskip("psycopg")
dotenv = pytest.importorskip("dotenv")
openpyxl = pytest.importorskip("openpyxl")

ROOT = Path(__file__).resolve().parents[1]
dotenv.load_dotenv(ROOT / ".env")
DB_URL = os.environ.get("SUPABASE_DB_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="SUPABASE_DB_URL not set")

NEW_FORMAT = ["NTB2614LHART00034", "NTB2521ZAPAT00053"]


# ---------------------------------------------------------------- main.py (no database)

@pytest.fixture
def fake_pipeline(monkeypatch, tmp_path):
    calls = []
    project = SimpleNamespace(project_id="NTB2614LHART00034", notes=[], bids=[1, 2, 3], items=[1],
                              low_bid=SimpleNamespace(bidder="Delta", base_bid=1.0))
    monkeypatch.setattr(main.extractor, "extract_project", lambda pdf, listing: project)
    monkeypatch.setattr(main.extractor, "validate", lambda p: [])
    monkeypatch.setattr(main.reports, "write_summary_excel", lambda p, source_url: tmp_path / "s.xlsx")
    monkeypatch.setattr(main.reports, "write_summary_pdf", lambda p, source_url: tmp_path / "s.pdf")

    def upsert(p, source_url):
        calls.append("workbook")
        return tmp_path / "master.xlsx"
    monkeypatch.setattr(main.master_sheet, "upsert_project", upsert)

    def sync(pid):
        calls.append(("db", pid))
        return True
    monkeypatch.setattr(main, "sync_to_db", sync)
    return calls


def test_process_loads_project_after_workbook(fake_pipeline):
    assert main.process(Path("x.pdf"), None) is True
    assert fake_pipeline == ["workbook", ("db", "NTB2614LHART00034")]


def test_process_no_db_is_workbook_only(fake_pipeline):
    assert main.process(Path("x.pdf"), None, sync_db=False) is True
    assert fake_pipeline == ["workbook"]


def test_db_failure_keeps_workbook_and_fails_run(fake_pipeline, monkeypatch):
    monkeypatch.setattr(main, "sync_to_db", lambda pid: fake_pipeline.append(("db", pid)) or False)
    assert main.process(Path("x.pdf"), None) is False
    assert fake_pipeline[0] == "workbook"


def test_no_db_flag_parses(monkeypatch):
    monkeypatch.setattr("sys.argv", ["main.py", "--pdf", "x.pdf", "--no-db"])
    assert main.parse_args().no_db is True
    monkeypatch.setattr("sys.argv", ["main.py", "--pdf", "x.pdf"])
    assert main.parse_args().no_db is False


def test_sync_to_db_reports_errors(monkeypatch, caplog):
    import ingest.sync_project as sp

    def boom(pid, verbose):
        raise SyncError("NOPE is not in data/project_airports.csv")
    monkeypatch.setattr(sp, "sync_project", boom)
    assert main.sync_to_db("NOPE") is False
    assert "database not updated" in caplog.text and "project_airports.csv" in caplog.text


def test_project_not_in_workbook():
    if not DB_URL:
        pytest.skip("SUPABASE_DB_URL not set")
    with pytest.raises(SyncError, match="NOPE123 is not in master_unit_prices.xlsx"):
        sync_project("NOPE123", dry_run=True)


# ---------------------------------------------------------------- database

@needs_db
@pytest.mark.parametrize("pid", NEW_FORMAT)
def test_rerun_new_format_project_changes_nothing(pid):
    s = sync_project(pid, dry_run=True, verbose=False)
    assert sum(s["changes"].values()) == 0, {k: v for k, v in s["changes"].items() if v}
    assert s["lines_rematched"] == 0
    assert s["review_queue"] == 0
    assert sum(s["match_methods"].values()) > 0 and "(none)" not in s["match_methods"]


@needs_db
def test_new_lines_are_matched_or_queued():
    """A line the normalizer has never seen gets matched (or queued) by the sync, not left blank."""
    pid = NEW_FORMAT[0]
    from ingest.load_catalog import CATALOG_CSV, match_line_items, read_catalog
    with psycopg.connect(DB_URL, autocommit=True) as c:
        with c.transaction(force_rollback=True):
            # forget the matches of this project's lines, as if they were freshly loaded
            c.execute("""update public.bid_line_items li set pay_item_id = null, match_method = null,
                                match_score = null, needs_review = false, review_notes = null
                         from public.bids b join public.projects p on p.id = b.project_id
                         where li.bid_id = b.id and p.project_id = %s and li.match_method <> 'manual'""", (pid,))
            # and give one of them a description no alias knows
            c.execute("""update public.bid_line_items li set raw_description = 'Gold-plated runway widget'
                         where li.id = (select li2.id from public.bid_line_items li2
                                        join public.bids b on b.id = li2.bid_id
                                        join public.projects p on p.id = b.project_id
                                        where p.project_id = %s order by li2.id limit 1)""", (pid,))
            _, _, check_notes = read_catalog(CATALOG_CSV)
            changed = match_line_items(c, check_notes, pid)
            n_lines, blank, queued = c.execute("""
                select count(*), count(*) filter (where li.match_method is null),
                       (select count(*) from public.v_review_queue q where q.project_id = %s)
                from public.bid_line_items li join public.bids b on b.id = li.bid_id
                join public.projects p on p.id = b.project_id where p.project_id = %s""", (pid, pid)).fetchone()
    assert changed == n_lines and blank == 0
    assert queued >= 1


@needs_db
def test_blank_source_pdf_keeps_stored_one():
    """main.py --pdf (offline) writes no Source PDF; the loader must not erase the stored link."""
    from ingest.load_master import WORKBOOK, build_projects, load
    projects, names, _ = build_projects(openpyxl.load_workbook(WORKBOOK, read_only=True, data_only=True))
    proj = next(p for p in projects if p["project_id"] == NEW_FORMAT[0])
    proj["source_pdf"] = None
    with psycopg.connect(DB_URL, autocommit=True) as c:
        with c.transaction(force_rollback=True):
            before = c.execute("select source_pdf from public.projects where project_id = %s",
                               (proj["project_id"],)).fetchone()[0]
            changes = load(c, [proj], names, verbose=False)
            after = c.execute("select source_pdf from public.projects where project_id = %s",
                              (proj["project_id"],)).fetchone()[0]
    assert before and after == before
    assert changes["projects.updated"] == 0
