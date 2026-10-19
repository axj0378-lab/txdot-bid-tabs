"""Unit tests for parsing, OCR reconciliation and master-sheet upsert (no OCR/browser needed).

Run:  python -m pytest -q
"""

from datetime import datetime

from openpyxl import load_workbook

from txdot_bid_tabs.extractor import (RawRow, _consensus_rows, _map_columns, match_bidders, normalize_item, parse_money,
                                      parse_qty, reconcile, split_description_code)
from txdot_bid_tabs.master_sheet import upsert_project
from txdot_bid_tabs.models import Bid, BidItem, Project


def test_parse_money():
    assert parse_money("$150,000.00") == 150000.0
    assert parse_money("$1.50") == 1.5
    assert parse_money("$15000000") == 150000.0   # OCR dropped the decimal point
    assert parse_money("") is None


def test_parse_qty():
    assert parse_qty("18,750") == 18750
    assert parse_qty("") is None


def test_normalize_item():
    assert normalize_item("F-164,2") == "F-164.2"
    assert normalize_item("F-164.3C") == "F-164.3c"
    assert normalize_item("tx-100") == "TX-100"
    assert normalize_item("C105") == "C-105"


def test_split_description_code():
    # Zapata-style descriptions, as cleaned (upper-cased) OCR text
    assert split_description_code("C-105, CONTRACTOR MOBILIZATION (8%)") == \
        ("C-105", "CONTRACTOR MOBILIZATION (8%)")
    assert split_description_code("ITEM P-152-4.1, DRAINAGE EXCAVATION AND TRENCHING") == \
        ("P-152-4.1", "DRAINAGE EXCAVATION AND TRENCHING")
    assert split_description_code("TEM L-110-5.1, INSTALL NON-ENCASED CONDUIT")[0] == "L-110-5.1"
    assert split_description_code("ITEM PARKHILL P-140-3.1, TEMPORARY BARRICADES")[0] == "P-140-3.1"
    assert split_description_code("ITEM MC1-1, FURNISH AND INSTALL TIE-DOWN ANCHORS")[0] == "MC1-1"
    assert split_description_code("C-102-5.1A, TEMPORARY AIR AND WATER POLLUTION")[0] == "C-102-5.1a"
    assert split_description_code("ITEM L-109-PLANS, FURNISH AND INSTALL")[0] == "L-109-PLANS"
    assert split_description_code("TXDOT ITEM 247-6.1, GRADE 1-2, TYPE A") == \
        ("TX-247-6.1", "GRADE 1-2, TYPE A")
    assert split_description_code("TXDOT ITEM 341 HOT MIX ASPHALT, TYPE D") == \
        ("TX-341", "HOT MIX ASPHALT, TYPE D")
    # Lockhart-style descriptions carry no code
    assert split_description_code("INSTALL 8 FT TALL GAME FENCING") is None
    assert split_description_code("MOBILIZATION") is None


def test_map_columns_recovers_one_misread_header():
    header = ["Item", "aty", "Unit", "Description", "Unit Price", "Total Price"]
    assert _map_columns(header) == {"item": 0, "qty": 1, "unit": 2, "description": 3,
                                    "unit_price": 4, "total": 5}


def test_reconcile_fixes_total_when_unit_price_unreadable():
    # Zapata: unit price blank, $11,810.00 total read as $11,810.06 (23,620 SY x $0.50)
    rows = [RawRow("P-152-4.6", 23620, "SY", "TOPSOIL REMOVAL", None, 11810.06)]
    up, tot, verified, _ = reconcile(rows, base_bid=11810.00)
    assert verified and up["P-152-4.6"] == 0.5 and tot["P-152-4.6"] == 11810.00


def test_reconcile_solves_single_line_from_base_bid():
    # Zapata, Sterling: $500.56 / $5,506.16 read as $500.50 / $5,506.10 (qty 11)
    rows = [RawRow("C-105", 1, "LS", "MOBILIZATION", 1000, 1000),
            RawRow("L-125.2", 11, "EA", "REMOVE SIGNAGE", 500.50, 5506.10)]
    up, tot, verified, notes = reconcile(rows, base_bid=6506.16)
    assert verified and up["L-125.2"] == 500.56 and tot["L-125.2"] == 5506.16
    assert any("solved from the base bid" in n for n in notes)
    # a shortfall far from anything OCR read is not papered over
    _, _, verified, _ = reconcile(rows, base_bid=9506.16)
    assert not verified


def test_consensus_qty_prefers_price_consistency():
    notes = []
    tables = [[RawRow("L-125.2", None, "EA", "X", 660.0, 7260.0)],
              [RawRow("L-125.2", 11, "EA", "X", 600.0, 6600.0)],
              [RawRow("L-125.2", 1, "EA", "X", 500.56, 5506.16)]]
    assert _consensus_rows(tables, notes)[0].qty == 11


def test_match_bidders():
    form = ["Sterling Delaware Holding Company", "Anderson Columbia Company, Inc.",
            "Foremost Paving, Inc."]
    ocr = ["Foremost Paving Inc.", "ANDERSON COLUMBIA CO., INC.", "Sterling Delaware Holding Company"]
    assert match_bidders(ocr, form) == {
        "Foremost Paving Inc.": "Foremost Paving, Inc.",
        "ANDERSON COLUMBIA CO., INC.": "Anderson Columbia Company, Inc.",
        "Sterling Delaware Holding Company": "Sterling Delaware Holding Company",
    }
    # badly garbled name, the only one left on each side: paired by elimination
    assert match_bidders(["Acme LLC", "Xq#rl ~~"], ["Acme LLC", "Delta Specialty"])["Xq#rl ~~"] \
        == "Delta Specialty"


def test_reconcile_fixes_misread_total():
    # Real misread seen on the Lockhart scan: 3 x $3,200 printed as $9,660.
    rows = [RawRow("C-105", 1, "LS", "MOBILIZATION", 150000, 150000),
            RawRow("F-164.3b", 3, "EA", "SWING GATE", 3200, 9660)]
    up, tot, verified, notes = reconcile(rows, base_bid=159600)
    assert verified
    assert tot["F-164.3b"] == 9600 and up["F-164.3b"] == 3200
    assert notes  # the correction is logged


def test_reconcile_fixes_misread_unit_price():
    rows = [RawRow("F-164.2", 17730, "LF", "FENCING", 84.0, 602820)]  # 34 misread as 84
    up, tot, verified, _ = reconcile(rows, base_bid=602820)
    assert verified and up["F-164.2"] == 34.0


def test_reconcile_flags_unverifiable():
    rows = [RawRow("C-105", 1, "LS", "MOBILIZATION", 100, 100)]
    _, _, verified, notes = reconcile(rows, base_bid=999)
    assert not verified and any("NOT verified" in n for n in notes)


def _project(pid, price):
    items = [BidItem("C-105", "MOBILIZATION", "LS", 1)]
    bids = [Bid("Low Co", price, {"C-105": price}, {"C-105": price}, True),
            Bid("High Co", price * 2, {"C-105": price * 2}, {"C-105": price * 2}, True)]
    return Project(pid, "Test Municipal Airport", "1234TEST", "Paving", "", datetime(2026, 10, 1),
                   items=items, bids=bids)


def test_master_upsert_has_no_duplicates(tmp_path):
    path = tmp_path / "master.xlsx"
    upsert_project(_project("NTB1", 100.0), path)
    upsert_project(_project("NTB1", 120.0), path)   # re-run same project with new data
    upsert_project(_project("NTB2", 200.0), path)
    wb = load_workbook(path)
    data = list(wb["Bid Data"].iter_rows(min_row=2, values_only=True))
    assert len(data) == 4                                  # 2 projects x 2 bidders x 1 item
    assert {r[0] for r in data} == {"NTB1", "NTB2"}
    ntb1_low = [r for r in data if r[0] == "NTB1" and r[6] == "Yes"][0]
    assert ntb1_low[11] == 120.0                            # replaced, not appended
    assert wb["Projects"].max_row == 3


def test_master_preserves_region(tmp_path):
    path = tmp_path / "master.xlsx"
    upsert_project(_project("NTB1", 100.0), path)
    wb = load_workbook(path)
    ws = wb["Projects"]
    region_col = [c.value for c in ws[1]].index("Region") + 1
    ws.cell(row=2, column=region_col, value="Central")     # user fills in a region
    wb.save(path)
    upsert_project(_project("NTB1", 110.0), path)
    ws = load_workbook(path)["Projects"]
    assert ws.cell(row=2, column=region_col).value == "Central"
