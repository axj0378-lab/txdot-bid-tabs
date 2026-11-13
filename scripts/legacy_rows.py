"""Stage 2 for legacy bid tabs: turn cached words into candidate bid-item rows.

Layout-agnostic trick: every consultant lays a tab out differently, but each
priced row contains a quantity followed by (unit price, amount) pairs, one pair
per bidder, where qty x unit price = amount. So for each text line we find the
numbers, pick the quantity that makes the most pairs reconcile, and read the
bidders' prices from those pairs. Rows where the arithmetic doesn't work are
kept but flagged, never silently fixed.

usage: python scripts/legacy_rows.py out/legacy_words/<stem> [--pages 1-3]
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ITEM_RE = re.compile(r"^(?:[A-Z]{1,4}[-‐–\.]?\d{1,4}[A-Za-z0-9\-‐–\.\(\)]*|\d{1,3}[A-Za-z]?\.?)$")
UNIT_WORDS = {
    "LS": "LS", "L.S.": "LS", "L.S": "LS", "LUMP": "LS", "LUMPSUM": "LS", "EA": "EA", "EA.": "EA",
    "EACH": "EA", "LF": "LF", "L.F.": "LF", "SY": "SY", "S.Y.": "SY", "SF": "SF", "S.F.": "SF",
    "CY": "CY", "C.Y.": "CY", "TON": "TON", "TONS": "TON", "GAL": "GAL", "GAL.": "GAL",
    "LB": "LB", "LBS": "LB", "AC": "AC", "ACRE": "AC", "MO": "MO", "MONTH": "MO", "DAY": "DAY",
    "DAYS": "DAY", "HR": "HR", "SFT": "SF", "SQYD": "SY", "SQ.YD.": "SY", "SQFT": "SF",
    "ALLOW": "ALLOW", "ALLOWANCE": "ALLOW", "MGAL": "MGAL", "STA": "STA", "LIN.FT.": "LF",
    "FA": "FA", "CF": "CF", "MI": "MI", "LNFT": "LF", "UNIT": "EA", "SET": "EA", "LOT": "LS",
}
NUM_RE = re.compile(r"^\(?-?\$?\(?-?[\d,]*\.?\d+\)?$")


def parse_num(tok: str) -> float | None:
    t = tok.replace("$", "").replace(",", "").strip()
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    if not re.fullmatch(r"-?\d*\.?\d+", t):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


@dataclass
class Line:
    page: int
    y: float
    words: list  # [x0,y0,x1,y1,text,conf]

    @property
    def text(self) -> str:
        return " ".join(w[4] for w in self.words)


@dataclass
class Row:
    page: int
    y: float
    item: str
    description: str
    unit: str | None
    qty: float | None
    pairs: list = field(default_factory=list)   # [(unit_price, amount, ok, x_center)]
    numbers: list = field(default_factory=list)
    text: str = ""
    qty_x: float = 0.0          # left edge of the quantity: descriptions sit left of it
    height: float = 30.0        # typical word height on this line
    schedule: str = ""

    @property
    def n_ok(self) -> int:
        return sum(1 for p in self.pairs if p[2])


def load_lines(folder: Path, pages: set[int] | None = None) -> list[Line]:
    lines: list[Line] = []
    for f in sorted(folder.glob("p*.json")):
        pg = int(f.stem[1:])
        if pages and pg not in pages:
            continue
        d = json.loads(f.read_text())
        words = sorted(d["words"], key=lambda w: (w[1] + w[3]) / 2)
        cur: list = []
        for w in words:
            yc = (w[1] + w[3]) / 2
            h = max(w[3] - w[1], 8)
            if cur and abs(yc - cur[-1][0]) > 0.55 * h:
                lines.append(Line(pg, cur[-1][0], sorted((c[1] for c in cur), key=lambda q: q[0])))
                cur = []
            cur.append((yc if not cur else (cur[-1][0] * len(cur) + yc) / (len(cur) + 1), w))
        if cur:
            lines.append(Line(pg, cur[-1][0], sorted((c[1] for c in cur), key=lambda q: q[0])))
    return lines


def _merge_money_tokens(words: list) -> list:
    """OCR often splits '$ 1,234.00' or '1,234 .00'. Join a lone '$' with what follows."""
    # split tokens glued by a table border: '28,750.00|$' -> '28,750.00', '$'
    split = []
    for w in words:
        parts = [x.strip() for x in re.split(r"\|+", w[4]) if x.strip()]
        if len(parts) <= 1:
            split.append([w[0], w[1], w[2], w[3], parts[0] if parts else w[4], w[5]])
            continue
        step = (w[2] - w[0]) / len(parts)
        for k, part in enumerate(parts):
            split.append([w[0] + k * step, w[1], w[0] + (k + 1) * step - 1, w[3], part, w[5]])
    # '4s 1,000.00': OCR glued the '$' onto the quantity (only when a dollar amount follows)
    glued = []
    for k, w in enumerate(split):
        m = re.fullmatch(r"(\d{1,3}(?:,\d{3})*)[Ss]", w[4])
        nxt = split[k + 1][4] if k + 1 < len(split) else ""
        prv = split[k - 1][4] if k else ""
        if m and re.fullmatch(r"\$?[\d,]*\d\.\d\d", prv.lstrip("S$")):
            glued.append([w[0], w[1], w[2], w[3], "$", w[5]])     # '$ 4.33 5S 5,001.15': '5S' is '$'
        elif m and re.fullmatch(r"\$?[\d,]+\.\d\d", nxt):
            mid = (w[0] + w[2]) / 2
            glued.append([w[0], w[1], mid, w[3], m.group(1), w[5]])
            glued.append([mid + 1, w[1], w[2], w[3], "$", w[5]])
        else:
            glued.append(w)
    words = glued
    out = []
    for w in words:
        t = re.sub(r"^[=S§s5]*\$(?=[\d(-])", "$", w[4])       # OCR: 'S$1.00', '§$1.00', '=$1.00'
        t = re.sub(r"^\$?[S§](?=\d)", "$", t) if re.fullmatch(r"\$?[S§][\d,.]+", t) else t
        t = re.sub(r"^[S§](?=\(\d)", "$", t)                     # '§(3,368.96)'
        t = re.sub(r"^(\$?[\d,]*\d\.\d\d)\.$", r"\1", t)             # '2.57.' -> '2.57'
        if t in ("S", "S$", "§", "$$", "=$", "s$", "5$"):
            t = "$"
        w = [w[0], w[1], w[2], w[3], t, w[5]]
        if out and out[-1][4] == "$" and w[0] - out[-1][2] < 60:
            p = out.pop()
            w = [p[0], min(p[1], w[1]), w[2], max(p[3], w[3]), w[4], w[5]]
        # '1 82,287.50' / '1 ,059,184.00': a 1-3 digit fragment glued to the next
        # numeric token when the gap is tiny (text-layer kerning artifact)
        if out and re.fullmatch(r"\$?\d{1,3}", out[-1][4]) and re.match(r"^[\d,.]", w[4]) \
                and ("," in w[4] or "." in w[4]) and -8 <= w[0] - out[-1][2] < 1.6 * (w[3] - w[1]):
            p = out.pop()
            w = [p[0], min(p[1], w[1]), w[2], max(p[3], w[3]), p[4] + w[4], w[5]]
        out.append(w)
    # a lone '5'/'3'/'8' between two money values is a misread '$' ('$ 1.84 5 107,842.40')
    for k in range(1, len(out) - 1):
        if out[k][4] in ("5", "3", "8") and re.fullmatch(r"\$?[\d,]+\.\d\d", out[k + 1][4]) \
                and "," in out[k + 1][4] and re.fullmatch(r"\$?[\d,]*\.\d\d", out[k - 1][4]):
            out[k] = [out[k][0], out[k][1], out[k][2], out[k][3], "$", out[k][5]]
    return out


def close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1.01, 0.002 * abs(b))


def parse_line(line: Line) -> Row | None:
    words = _merge_money_tokens(line.words)
    toks = [w[4] for w in words]
    nums = []
    unit = None
    unit_idx = None
    for k, w in enumerate(words):
        t = w[4].upper().strip("|")
        if unit is None and t in UNIT_WORDS:
            unit, unit_idx = UNIT_WORDS[t], k
        v = parse_num(w[4]) if NUM_RE.match(w[4].strip("|")) else None
        if v is not None:
            nums.append((k, v, (w[0] + w[2]) / 2, w[4]))
    if len(nums) < 3 or not any("$" in n[3] or re.search(r"\.\d\d$", n[3]) for n in nums):
        return None
    best = None
    # candidate qty: any number; pairs are consecutive numbers after it
    for qi in range(len(nums) - 2):
        q = nums[qi][1]
        if q == 0:
            continue
        pairs = []
        j = qi + 1
        while j + 1 < len(nums):
            up, amt = nums[j][1], nums[j + 1][1]
            ok = close(q * up, amt)
            pairs.append((up, amt, ok, nums[j][2]))
            j += 2
        score = sum(p[2] for p in pairs)
        if best is None or score > best[0]:
            best = (score, qi, pairs)
    # quantities shown rounded by cell formatting ("9" for 8.7): if every pair after
    # a small number gives the same amount / unit price, use that as the quantity
    if not best or best[0] < max(1, len(best[2]) // 2):
        for qi in range(len(nums) - 2):
            q = nums[qi][1]
            if q == 0 or abs(q) >= 100000:
                continue
            seq = nums[qi + 1:]
            pr = [(seq[j][1], seq[j + 1][1], seq[j][2]) for j in range(0, len(seq) - 1, 2)]
            ratios = [b / a for a, b, _ in pr if a > 0 and b != 0]
            if len(ratios) >= 2 and max(ratios) - min(ratios) <= 0.002 * max(abs(r) for r in ratios) \
                    and abs(ratios[0] - q) <= 0.5 and ratios[0] != q:
                qtrue = round(sum(ratios) / len(ratios), 3)
                pairs = [(a, b, close(qtrue * a, b), x) for a, b, x in pr]
                best = (sum(p[2] for p in pairs), qi, pairs)
                nums[qi] = (nums[qi][0], qtrue, nums[qi][2], nums[qi][3])
                break
    if not best or best[0] == 0:
        return None
    _, qi, pairs = best
    qk = nums[qi][0]
    lead = [w[4] for w in words[:qk]]
    item = lead[0] if lead and ITEM_RE.match(lead[0].strip("|")) else ""
    if item and len(lead) > 1 and re.fullmatch(r"\d{1,3}", lead[0]) and ITEM_RE.match(lead[1]) \
            and not re.fullmatch(r"\d{1,3}", lead[1]):
        item = lead[1]  # "1  C-105" : line number then spec item
        desc_words = lead[2:]
    else:
        desc_words = lead[1:] if item else lead
    desc_words = [t for k, t in enumerate(desc_words) if not (unit_idx is not None and t.upper().strip("|") in UNIT_WORDS
                                                               and k == len(desc_words) - 1)]
    if not desc_words:
        # 'ITEM QTY UNIT DESCRIPTION PRICE...' layouts: description sits between the
        # quantity/unit and the first price
        first_price = nums[qi + 1][0] if qi + 1 < len(nums) else len(words)
        desc_words = [w[4] for k, w in enumerate(words[qk + 1:first_price], start=qk + 1)
                      if k != unit_idx and w[4] not in ("$",)]
    heights = sorted(w[3] - w[1] for w in words)
    return Row(line.page, line.y, item.strip("|"), " ".join(desc_words).strip(" |"), unit, nums[qi][1],
               pairs, [n[1] for n in nums], line.text, words[qk][0], heights[len(heights) // 2])


def rows_for(folder: Path, pages: set[int] | None = None) -> list[Row]:
    return [r for r in (parse_line(l) for l in load_lines(folder, pages)) if r]


def _pages(spec: str | None) -> set[int] | None:
    if not spec:
        return None
    out = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out |= set(range(int(a), int(b or a) + 1))
    return out


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    pages = _pages(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[2] == "--pages" else None
    for r in rows_for(folder, pages):
        pr = "  ".join(f"{p[0]:,.2f}/{p[1]:,.2f}{'' if p[2] else '?'}" for p in r.pairs)
        print(f"p{r.page} {r.item:<12} {r.unit or '-':<5} {r.qty:>10,.2f} | {r.description[:40]:<40} | {pr}")
