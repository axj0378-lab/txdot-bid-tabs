"""Unit tests for ingest/normalize.py (SPEC.md "Pay item normalizer"). No database needed."""

import pytest

from ingest.normalize import CatalogItem, clean_code, clean_description, item_key, match_line, normalize_line

PLACES = {"GRANBURY", "TERRELL", "WESLACO", "KINGSVILLE", "FALFURRIAS", "COMMERCE", "EDNA", "BONHAM"}


# ---------------------------------------------------------------- codes

@pytest.mark.parametrize("raw, section", [
    ("P-401-5.1", "P-401"),
    ("P-401", "P-401"),
    ("L-125-5.6", "L-125"),
    ("L-125l", "L-125"),            # 12th item of an a..n sequence, not an OCR 1
    ("L108-5.1", "L-108"),          # missing dash
    ("P-4O1-5.1", "P-401"),         # OCR O in a number
    ("P-4l1", "P-411"),             # OCR l in a number
    ("P–620-5.1", "P-620"),    # en dash
    ("TX-341.5.1", "TX-341"),
    ("TXDOT341", "TX-341"),
    ("TXDOT-247", "TX-247"),
    ("TXDOT341-6", "TX-341"),
    ("ITEM360.6", "TX-360"),
    ("SS-120", "SS-120"),
    ("SS-3076-6.1", "SS-3076"),
    ("KSA-701-5.2", "KSA-701"),
    ("2509TEMPL#P-101", "P-101"),   # project prefix stripped
])
def test_clean_code_section(raw, section):
    assert clean_code(raw).spec_section == section


@pytest.mark.parametrize("raw", [
    "1.04", "2509TEMPL#1.04", "2512GLVST#A1.03", "2521EDINB#14", "2509TEMPL#?",
    "A-1.01", "A-2.16", "AG1-1", "AB1-1", "MC1-1", "S-1-2.1", "S1-A1-8", "SS-1-1",
    "P-43",                          # 2-digit section: OCR damage, match on description
    None, "",
])
def test_clean_code_no_section(raw):
    assert clean_code(raw).spec_section is None


# ---------------------------------------------------------------- descriptions

@pytest.mark.parametrize("raw, expected", [
    ("Mobilization - Granbury", "MOBILIZATION"),
    ("Seeding - Edna", "SEEDING"),
    ("Clean and Seal Joints and Cracks > 1 1/2\" (Weslaco aprons, deduct)",
     "CLEAN AND SEAL JOINTS AND CRACKS > 1 1/2 IN"),
    ("Refined Coal Tar Emulsion with Additives for Slurry Coat (credit)",
     "REFINED COAL TAR EMULSION WITH ADDITIVES FOR SLURRY COAT"),
    ("2 Inch Asphalt Pavement (deduct)", "2 IN ASPHALT PAVEMENT"),
    ("Remove 2-inch conduit including cable(s)", "REMOVE 2 IN CONDUIT INCLUDING CABLE(S)"),
    ("6' Wrought Iron Fence w/ Mow Strip", "6 FT WROUGHT IRON FENCE WITH MOW STRIP"),
    ("Install RW Closure Markers (Special)", "INSTALL RW CLOSURE MARKERS"),
    ("Clean and Seal Joints (Commerce apron)", "CLEAN AND SEAL JOINTS"),
    ("Paint  Markings,   White & Yellow", "PAINT MARKINGS WHITE AND YELLOW"),
    ("8\" Concrete Pavement -", "8 IN CONCRETE PAVEMENT"),
    ("Taxiway Light (Mid-Way taxiway)", "TAXIWAY LIGHT (MID-WAY TAXIWAY)"),   # not a known place: kept
    ("Cold Milling (0\" - 2\") - Deduct", "COLD MILLING (0 IN - 2 IN)"),
    ("Plan Utility Allowance NTE $100,000.00", "PLAN UTILITY ALLOWANCE NTE $100000.00"),
    ("No.8 AWG L-824C, 5 kV", "NO 8 AWG L-824C 5KV"),
    ("Cement Treated Subgrade (12\")", "CEMENT TREATED SUBGRADE (12 IN)"),
    ("� Rock Filter Dam", "ROCK FILTER DAM"),
    ("“Type D” Hot Mix", "\"TYPE D\" HOT MIX"),
])
def test_clean_description(raw, expected):
    assert clean_description(raw, PLACES) == expected


# ---------------------------------------------------------------- code embedded in the description

@pytest.mark.parametrize("code, desc, section, expected_desc", [
    ("2612ANGLE#?", "1.68 1L-110-5.4 4-Way, 2\" Sch. 40 PVC Conduit, Concrete Encased, in Turf",
     "L-110", "4-WAY 2 IN SCH 40 PVC CONDUIT CONCRETE ENCASED IN TURF"),
    ("2611NACOG#?", "A - 2.04 P-101-5.2 Crack Repair (Less than 1\" Wide)",
     "P-101", "CRACK REPAIR (LESS THAN 1 IN WIDE)"),
    ("A-2.16", "P-620-5.7g Surface Preparation", "P-620", "SURFACE PREPARATION"),
    ("2501DENSN-JUN25#?", "L-125d_ _ Install L-861 LED Runway Edge Light",
     "L-125", "INSTALL L-861 LED RUNWAY EDGE LIGHT"),
    ("2509TEMPL#?", "Pea Loch ne r 1.60 L-105 Remove and Dispose 2-Way JCP",
     "L-105", "REMOVE AND DISPOSE 2-WAY JCP"),
    ("S1-A1-8", "Joint Sealants for Pavements HMAC P-605.5.1 -", "P-605", "JOINT SEALANTS FOR PAVEMENTS HMAC"),
    # equipment types are not spec sections
    ("2521EDINB#3", "Install L-861 Edge Light", None, "INSTALL L-861 EDGE LIGHT"),
    ("2521EDINB#4", "No. 8 AWG, L-824C Cable", None, "NO 8 AWG L-824C CABLE"),
])
def test_section_from_description(code, desc, section, expected_desc):
    n = normalize_line(code, desc, "EA", PLACES)
    assert n.spec_section == section
    assert n.description == expected_desc


def test_code_section_wins_over_description():
    n = normalize_line("L-125-5.6", "Install New L-861(L) Elevated Light in Existing L-867B Base Can", "EA", PLACES)
    assert n.spec_section == "L-125"


# ---------------------------------------------------------------- matching

CATALOG = [
    CatalogItem(1, "C-105", "MOBILIZATION", "LS"),
    CatalogItem(2, "P-101", "CLEAN AND SEAL JOINTS AND CRACKS 1/4 IN TO 1 1/2 IN", "LF"),
    CatalogItem(3, "P-101", "CLEAN AND SEAL JOINTS AND CRACKS > 1 1/2 IN", "LF"),
    CatalogItem(4, "T-904", "SODDING", "SY"),
    CatalogItem(5, "P-620", "RUNWAY AND TAXIWAY PAINTING", "SF"),
    CatalogItem(6, "P-620", "RUNWAY AND TAXIWAY PAINTING", "LS"),
]


def _match(code, desc, unit, aliases=None):
    return match_line(normalize_line(code, desc, unit, PLACES), CATALOG, aliases or {})


def test_alias_exact():
    m = _match("2509TEMPL#1.04", "Sod Placement", "SY", {("SOD PLACEMENT", "SY", None): 4})
    assert (m.pay_item_id, m.method, m.score, m.needs_review) == (4, "alias", 1.0, False)


def test_alias_with_section_beats_alias_without():
    aliases = {("SODDING", "SY", "T-904"): 4, ("SODDING", "SY", None): 99}
    assert _match("T-904-5.1", "Sodding", "SY", aliases).pay_item_id == 4


def test_exact_within_section_and_unit():
    m = _match("C-105-6.6", "Mobilization - Terrell", "LS")
    assert (m.pay_item_id, m.method, m.needs_review) == (1, "exact", False)


def test_unit_must_agree():
    assert _match("P-620-5.1", "Runway and Taxiway Painting", "LS").pay_item_id == 6
    assert _match("P-620-5.1", "Runway and Taxiway Painting", "SF").pay_item_id == 5
    m = _match("P-620-5.1", "Runway and Taxiway Painting", "EA")
    assert m.pay_item_id is None and m.method == "unmatched" and m.needs_review


def test_fuzzy_auto():
    m = _match("T-904-5.1", "Sodding, complete in place", "SY")
    assert m.pay_item_id == 4 and m.method == "fuzzy" and m.score >= 0.90 and not m.needs_review


def test_fuzzy_needs_review_band():
    m = _match("P-620-5.1", "Apron and Taxiway Painting", "SF")   # 0.87 vs RUNWAY AND TAXIWAY PAINTING
    assert m.pay_item_id == 5 and m.method == "fuzzy" and 0.75 <= m.score < 0.90 and m.needs_review


def test_different_numbers_never_auto_match():
    # token_set_ratio rates these as near-identical; the size range is what makes them different items
    m = _match("P-101-5.1", "Clean and Seal Joints and Cracks > 2\"", "LF")
    assert m.needs_review


def test_number_order_matters():
    catalog = [CatalogItem(7, "L-112", "2-WAY 4 IN HDPE CONDUIT", "LF")]
    m = match_line(normalize_line("L-112", "4-Way 2\" HDPE Conduit", "LF", PLACES), catalog, {})
    assert m.needs_review


def test_equipment_type_letter_matters():
    catalog = [CatalogItem(8, "L-115", "INSTALL L-867E JUNCTION BOX COMPLETE", "EA")]
    m = match_line(normalize_line("L-115", "Install L-867B Junction Box, Complete", "EA", PLACES), catalog, {})
    assert m.needs_review


@pytest.mark.parametrize("line, item", [
    ("Remove Existing Elevated Runway Edge Light", "REMOVE EXISTING ELEVATED TAXIWAY EDGE LIGHT"),
    ("Remove Elevated Edge Light", "REMOVE IN-PAVEMENT EDGE LIGHT"),
    ("Install Salvaged Beacon on Tip Down Pole", "INSTALL NEW BEACON ON TIP DOWN POLE"),
    ("Yellow Pavement Markings (Reflective)", "WHITE PAVEMENT MARKINGS (REFLECTIVE)"),
    ("Black Markings Non-Reflective", "BLACK MARKINGS REFLECTIVE"),
    ("Temporary Taxiway Markings", "TAXIWAY MARKINGS"),
    ("Install L-861 LED Runway Threshold Light", "INSTALL L-861 LED RUNWAY EDGE LIGHT"),
    ("L-858 Two Module Size 1 Sign", "L-858 ONE MODULE SIZE 1 SIGN"),
    ("Sealing Existing Open Cracks > 1 1/2\"", "SEALING EXISTING OPEN CRACKS < 1 1/2 IN"),
    ("Pavement Marking with Reflective Media", "PAVEMENT MARKINGS WITHOUT REFLECTIVE MEDIA"),
    ("Existing Base Mounted Edge Light Removed", "EXISTING BASE MOUNTED EDGE LIGHT RELOCATED"),
    ("Dense-Graded HMA (Type D)", "DENSE-GRADED HMA (TYPE C)"),
])
def test_contrast_words_never_auto_match(line, item):
    catalog = [CatalogItem(9, "L-105", item, "EA")]
    assert match_line(normalize_line("L-105", line, "EA", PLACES), catalog, {}).needs_review


@pytest.mark.parametrize("a, b", [
    ("SILT FENCE INSTALLATION AND REMOVAL", "INSTALLATION AND REMOVAL OF SILT FENCE"),
    ("REMOVE AND DISPOSE AIRFIELD SIGN AND FOUNDATION", "REMOVE AND DISPOSE OF AIRFIELD SIGN AND FOUNDATION"),
    ("PAVEMENT MARKINGS REMOVAL", "PAVEMENT MARKING REMOVAL"),
    ("ASPHALT SURFACE COURSE (4 IN)", "4 IN ASPHALT SURFACE COURSE"),
    ("8 IN - CONCRETE PAVEMENT", "8 IN CONCRETE PAVEMENT"),
    ("STORM WATER POLLUTION PREVENTION PLAN (SWPPP)", "STORM WATER POLLUTION PREVENTION PLAN"),
    ("CONTRACTOR QUALITY CONTROL PROGRAM (CQCP)", "CONTRACTOR QUALITY CONTROL PROGRAM"),
    ("INSTALL L-867B JUNCTION BOX COMPLETE", "INSTALL L-867B JUNCTION BOX"),
])
def test_item_key_same(a, b):
    assert item_key(a) == item_key(b)


@pytest.mark.parametrize("a, b", [
    ("4-WAY 2 IN HDPE CONDUIT", "2-WAY 4 IN HDPE CONDUIT"),
    ("INSTALL L-858 LED GUIDANCE SIGN 1 MODULE SIZE 2", "INSTALL L-858 LED GUIDANCE SIGN SIZE 1 2 MODULE"),
    ("INSTALL L-861 LED RUNWAY THRESHOLD LIGHT", "INSTALL L-861 LED RUNWAY EDGE LIGHT"),
    ("PAVEMENT MARKING WITH REFLECTIVE MEDIA", "PAVEMENT MARKING WITHOUT REFLECTIVE MEDIA"),
    ("SEALING EXISTING OPEN CRACKS > 1 1/2 IN", "SEALING EXISTING OPEN CRACKS < 1 1/2 IN"),
    ("ROCK FILTER DAM", "ROCK FILTER DAMS (TYPE 1) (INSTALL AND REMOVE)"),
    ("MOBILIZATION (QA)", "MOBILIZATION"),          # not the initials of the words before it
])
def test_item_key_different(a, b):
    assert item_key(a) != item_key(b)


def test_no_section_matches_on_description():
    m = _match("2512GLVST#1.07", "Mobilization", "LS")
    assert (m.pay_item_id, m.method, m.needs_review) == (1, "exact", False)


def test_section_with_no_catalog_items_falls_back_but_needs_review():
    m = _match("CC-105", "Mobilization - Bonham", "LS")    # CC-105: OCR for C-105
    assert m.pay_item_id == 1 and m.needs_review


def test_unmatched():
    m = _match("L-125-5.6", "Install REIL Unit", "EA")
    assert m.pay_item_id is None and m.method == "unmatched" and m.needs_review


def test_never_creates_pay_items():
    before = list(CATALOG)
    _match("X-999", "Something New", "EA")
    assert CATALOG == before
