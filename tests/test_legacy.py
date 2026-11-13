"""Legacy bid tabs: every extracted project must reconcile to the printed totals."""

import json

import pytest

from txdot_bid_tabs import legacy

FILES = sorted(legacy.LEGACY_DIR.glob("*.json"))
PENDING: set[str] = set()   # extracted but not yet reconciled


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_bidders_reconcile_to_printed_totals(path):
    project = legacy.load(path)
    if project.project_id in PENDING:
        pytest.skip("pending: line items don't reconcile yet")
    assert project.bids
    for bid in project.bids:
        assert bid.verified, f"{bid.bidder}: line items don't sum to the printed total"


def test_conroe_award_and_low_bidder():
    p = legacy.load(legacy.LEGACY_DIR / "2412CONRO.json")
    assert p.low_bid.bidder == "Hi-Lite Airfield Services LLC"
    assert p.low_bid.base_bid == 874967.60
    assert p.estimate is not None and p.estimate not in p.bids


def test_repeated_codes_keep_separate_prices():
    # Lufkin lists P-620 twice in one schedule; both rows must keep their own price
    p = legacy.load(legacy.LEGACY_DIR / "2511LUFKN.json")
    keys = [it.key for it in p.items]
    assert len(keys) == len(set(keys))


def test_bare_line_numbers_are_prefixed():
    assert legacy._item_code("1.04", "2509TEMPL") == "2509TEMPL#1.04"
    assert legacy._item_code("P-401-8.1", "2509TEMPL") == "P-401-8.1"


def test_no_formula_like_descriptions():
    for f in FILES:
        for it in json.loads(f.read_text(encoding="utf-8"))["items"]:
            assert not it["description"].startswith(("=", "+", "@")), (f.stem, it["item"])
