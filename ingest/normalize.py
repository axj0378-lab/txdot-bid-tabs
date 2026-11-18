"""Pay item normalizer (SPEC.md "Pay item normalizer").

normalize_line(raw code, raw description, unit, places) -> Normalized(spec_section, description, unit)
match_line(normalized, catalog, aliases)               -> Match(pay_item_id, method, score, needs_review)

Pure functions, no database access. Never creates pay items.
"""

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

AUTO_MATCH = 0.90
REVIEW_MATCH = 0.75

# Spec-section prefixes rewritten to one form: TXDOT-341 / ITEM 360 are TxDOT standard items.
PREFIX_ALIASES = {"TXDOT": "TX", "ITEM": "TX"}

# Section = letter prefix + 3-4 digit number. Shorter numbers are line numbers (A-1.01, SS-1-1)
# or OCR damage (P-43) and get no section.
_CODE_SECTION = re.compile(r"^([A-Z]{1,5})-?(\d{3,4})(?!\d)")
# Section written inside a description: dash required, optional OCR '1' in front (1L-110-5.4),
# optional sequence letter (L-125d) and subsection (-5.4, .5.1).
_DESC_SECTION = re.compile(r"(?<![A-Z0-9])1?([A-Z]{1,5})-(\d{3,4})(?!\d)([A-Z]?)((?:[-.]\d[\dA-Z.]*)?)[\s_]*")
_LINE_NUMBER_END = re.compile(r"\d+\.\d{2}$")
_TOKENS = re.compile(r"[A-Z0-9$][A-Z0-9$./-]*|[<>]")
_HAS_DIGIT = re.compile(r"\d")

# Words that make two otherwise similar items different. If one description has such a word and the
# other doesn't, they are never auto-matched. (Single letters and symbols, e.g. TYPE C/D or < / >,
# and number-bearing tokens count the same way.)
CONTRAST_WORDS = {
    "RUNWAY", "TAXIWAY", "TAXILANE", "APRON", "HELIPAD",
    "THRESHOLD", "EDGE", "CENTERLINE", "END",
    "ELEVATED", "IN-PAVEMENT", "SEMI-FLUSH", "BASE-MOUNTED", "STAKE-MOUNTED",
    "WHITE", "YELLOW", "BLACK", "RED", "GREEN", "BLUE",
    "REFLECTIVE", "NON-REFLECTIVE", "WITH", "WITHOUT",
    "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX",
    "REMOVE", "REMOVED", "REMOVAL", "INSTALL", "INSTALLED", "INSTALLATION", "RELOCATE", "RELOCATED",
    "SALVAGE", "SALVAGED", "NEW", "REPLACE", "REPLACED", "ABANDON", "ABANDONED",
    "TEMPORARY", "PERMANENT",
}

# Filler ignored by item_key()
_FILLER = {"OF", "THE", "AND", "A", "AN", "COMPLETE", "-"}


@dataclass(frozen=True)
class Code:
    code: str | None            # cleaned code, project prefix removed
    spec_section: str | None


@dataclass(frozen=True)
class Normalized:
    spec_section: str | None
    description: str
    unit: str | None


@dataclass(frozen=True)
class CatalogItem:
    id: int
    spec_section: str
    canonical_name: str
    unit_code: str


@dataclass(frozen=True)
class Match:
    pay_item_id: int | None
    method: str                 # alias | exact | fuzzy | unmatched
    score: float | None
    needs_review: bool


# ---------------------------------------------------------------- codes

def _fix_ocr(s):
    s = re.sub(r"[‐-―−]", "-", s)             # unicode dashes
    s = re.sub(r"[“”″]", '"', s)               # smart double quotes, double prime
    s = re.sub(r"[‘’′]", "'", s)               # smart single quotes, prime
    s = re.sub(r"[^\x20-\x7e]", " ", s)                       # anything else non-ASCII (OCR debris)
    s = re.sub(r"(?<=\d)[lI](?=\d)|(?<=-)[lI](?=\d)", "1", s)  # 4l1 -> 411; only next to digits
    s = re.sub(r"(?<=\d)O(?=\d)", "0", s)                     # 4O1 -> 401
    return s


def clean_code(raw):
    """Strip the project prefix (2509TEMPL#), fix OCR, and find the spec section."""
    if not raw or not str(raw).strip():
        return Code(None, None)
    s = _fix_ocr(str(raw).strip())
    if "#" in s:
        s = s.split("#", 1)[1]
    s = s.upper().replace(" ", "")
    if not s or s == "?":
        return Code(None, None)
    m = _CODE_SECTION.match(s)
    if not m:
        return Code(s, None)
    prefix = PREFIX_ALIASES.get(m.group(1), m.group(1))
    return Code(s, f"{prefix}-{m.group(2)}")


def _section_from_description(desc):
    """(section, rest) when the description carries the item code (OCR'd or bare-numbered tabs).

    Accepted only at the start (after an optional line number such as '1.68' or 'A - 2.04'), or
    anywhere when it has a subsection (P-605.5.1). Equipment types (L-861, L-824C) are neither.
    """
    for m in _DESC_SECTION.finditer(desc):
        before = desc[:m.start()].strip()
        at_start = not before or bool(_LINE_NUMBER_END.search(before))
        if at_start or m.group(4):
            prefix = PREFIX_ALIASES.get(m.group(1), m.group(1))
            rest = desc[m.end():] if at_start else desc[:m.start()] + " " + desc[m.end():]
            return f"{prefix}-{m.group(2)}", rest
    return None, desc


# ---------------------------------------------------------------- descriptions

def _place_pattern(places):
    names = sorted({p.upper() for p in places if p}, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b") if names else None


def clean_description(raw, places=()):
    """Upper case, collapse spaces, drop credit/deduct and location tags, standard abbreviations."""
    s = _fix_ocr(str(raw or "")).upper().replace("_", " ")
    place_re = _place_pattern(places)

    # Parentheticals that only say where or that it's a credit: (deduct), (Weslaco aprons, deduct), (Commerce apron)
    def drop_paren(m):
        inner = m.group(1)
        if re.search(r"\b(CREDIT|DEDUCT|SPECIAL)\b", inner) or (place_re and place_re.search(inner)):
            return " "
        return m.group(0)
    s = re.sub(r"\(([^()]*)\)", drop_paren, s)

    # Trailing location or credit: 'Mobilization - Granbury', 'Cold Milling (0" - 2") - Deduct'
    s = re.sub(r"\s+-\s*(?:CREDIT|DEDUCT)\s*$", "", s.rstrip())
    if place_re:
        m = re.search(r"\s+-\s+([^-]+)$", s)
        if m and place_re.fullmatch(m.group(1).strip()):
            s = s[:m.start()]

    s = s.replace("&", " AND ")
    s = re.sub(r"\bW/\s*", "WITH ", s)
    s = re.sub(r"\bINCL\b\.?", "INCLUDING", s)
    s = re.sub(r"\bNO\.?\s*(?=\d)", "NO ", s)                                # No.8, NO8 -> NO 8
    s = re.sub(r"\bNO\.", "NO", s)
    s = re.sub(r"\bSCH\.", "SCH", s)
    s = re.sub(r"\bRWY\b", "RUNWAY", s)
    s = re.sub(r"\bTWY\b", "TAXIWAY", s)
    s = re.sub(r"(\d)\s*KV\b", r"\1KV", s)                                    # 5 kV -> 5KV
    s = re.sub(r"(\d)\s*(?:\"|''|-?\s*INCH(?:ES)?\b|IN\.)", r"\1 IN ", s)    # 2", 2-inch, 2 Inch, 2 in.
    s = re.sub(r"(\d)\s*(?:'|-?\s*(?:FOOT|FEET)\b|FT\.)", r"\1 FT ", s)      # 6', 6-foot, 6 ft.
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)                                # 100,000 -> 100000
    s = re.sub(r"[,;:]", " ", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"(\s*-)+$", "", s).strip()                                    # '8" Concrete Pavement -'
    return s


def normalize_line(raw_code, raw_description, unit, places=()):
    code = clean_code(raw_code)
    desc = _fix_ocr(str(raw_description or "")).upper().replace("_", " ")
    desc = re.sub(r"\s+", " ", desc).strip()
    if code.spec_section:
        # The code wins. Drop the same code repeated at the start of the description, nothing else.
        section = code.spec_section
        lead = _DESC_SECTION.match(desc)
        if lead and f"{PREFIX_ALIASES.get(lead.group(1), lead.group(1))}-{lead.group(2)}" == section:
            desc = desc[lead.end():]
    else:
        section, desc = _section_from_description(desc)
    return Normalized(section, clean_description(desc, places), (unit or "").upper() or None)


# ---------------------------------------------------------------- matching

def _numbers(s):
    """Number-bearing tokens in order, letters kept: '4-WAY 2 IN L-867B' -> ['4-WAY', '2', 'L-867B']."""
    return [t for t in _TOKENS.findall(s) if _HAS_DIGIT.search(t)]


def _contrast(a, b):
    """Distinguishing words present in exactly one of the two descriptions."""
    diff = set(_TOKENS.findall(a)) ^ set(_TOKENS.findall(b))
    return {t for t in diff if t in CONTRAST_WORDS or len(t) == 1}


def _singular(word):
    return word[:-1] if len(word) > 3 and word.endswith("S") and not word.endswith("SS") else word


def item_key(desc):
    """Key for 'these two cleaned descriptions name the same item' (catalog suggestions).

    Same words in any order, ignoring filler (OF, THE, AND, COMPLETE), plurals and an acronym that
    repeats the words before it ('... PREVENTION PLAN (SWPPP)'); number-bearing tokens must also
    agree in order. Deliberately strict: no fuzzy similarity.
    """
    s = desc
    for m in reversed(list(re.finditer(r"\s*\(([A-Z]{2,8})\)", s))):
        initials = "".join(w[0] for w in re.findall(r"[A-Z][A-Z0-9-]*", s[:m.start()]) if w not in _FILLER)
        if initials.endswith(m.group(1)):
            s = s[:m.start()] + s[m.end():]
    tokens = [_singular(t) for t in _TOKENS.findall(s) if t not in _FILLER]
    # Each number with the word after it: '1 MODULE SIZE 2' != 'SIZE 1 2 MODULE', '(4 IN)' == '4 IN ...'
    numbers = tuple((t, tokens[i + 1] if i + 1 < len(tokens) else None)
                    for i, t in enumerate(tokens) if _HAS_DIGIT.search(t))
    return frozenset(tokens), numbers


def same_item_text(a, b):
    """True when two cleaned descriptions may name the same item: same numbers in order and no contrast word."""
    return _numbers(a) == _numbers(b) and not _contrast(a, b)


def match_line(n, catalog, aliases):
    """Alias exact -> exact name within section+unit -> fuzzy (token_set_ratio).

    aliases: {(description_normalized, unit_code, spec_section or None): pay_item_id}
    Fuzzy: >= 0.90 auto, 0.75-0.90 needs review, < 0.75 unmatched. A candidate whose numbers
    differ (sizes, depths, counts, equipment types such as L-867B/L-867E) or that differs by a
    CONTRAST_WORDS word (runway/taxiway, white/yellow, remove/install, ...) is never
    auto-matched. A section with no catalog items for the
    unit falls back to all items with that unit, and then always needs review.
    """
    for key in ((n.description, n.unit, n.spec_section), (n.description, n.unit, None)):
        if key in aliases:
            return Match(aliases[key], "alias", 1.0, False)

    same_unit = [c for c in catalog if c.unit_code == n.unit]
    pool, fallback = same_unit, False
    if n.spec_section:
        in_section = [c for c in same_unit if c.spec_section == n.spec_section]
        pool, fallback = (in_section, False) if in_section else (same_unit, True)
    if not pool:
        return Match(None, "unmatched", None, True)

    names = {c.id: clean_description(c.canonical_name) for c in pool}
    exact = [c for c in pool if names[c.id] == n.description]
    if len(exact) == 1:
        return Match(exact[0].id, "exact", 1.0, fallback)

    scored = sorted(
        ((same_item_text(n.description, names[c.id]), fuzz.token_set_ratio(n.description, names[c.id]) / 100, c)
         for c in pool),
        key=lambda t: (t[0], t[1]), reverse=True)
    same_text, score, best = scored[0]
    score = round(score, 4)
    if score < REVIEW_MATCH:
        return Match(None, "unmatched", score, True)
    tie = len(scored) > 1 and scored[1][:2] == scored[0][:2]
    review = score < AUTO_MATCH or not same_text or fallback or tie or len(exact) > 1
    return Match(best.id, "fuzzy", score, review)
