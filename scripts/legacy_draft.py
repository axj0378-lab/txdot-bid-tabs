"""Stage 3 for legacy bid tabs: draft the data/legacy/<id>.json file.

Reads parsed rows (legacy_rows.py), puts each (unit price, amount) pair under a
bidder column by its x position (learned from fully-priced rows), tags each row
with the bid schedule heading above it, and prints the TOTAL lines so the
printed totals can be copied in. The draft is then checked by hand.

usage:
  python scripts/legacy_draft.py STEM --cols "Engineer's Estimate|Bidder A|Bidder B"
         [--pages 1-3] [--schedule-re "BID SCHEDULE \\d|BASE BID|ALTERNATE \\d"] [--show]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from legacy_rows import Line, _pages, load_lines, parse_line  # noqa: E402

WORDS = Path(__file__).resolve().parent.parent / "out" / "legacy_words"
DEFAULT_SCHED = r"(BID\s+ADDITIVE\s*(NO\.?\s*)?\w+|ADDITIVE\s+ALTERNATE\s*(NO\.?\s*)?\w+|BID\s+SCHEDULE\s*(NO\.?\s*)?\w+|BASE\s+BID|ADD(ITIVE)?\.?\s+ALT(ERNATE)?\.?\s*(NO\.?\s*)?\w+|ALTERNATE\s*(NO\.?\s*)?\w+|SCHEDULE\s+[A-Z0-9]+)"


HEADER_WORDS = re.compile(r"\b(TOTAL|SUBTOTAL|SCHEDULE|DESCRIPTION|QUANTITY|PRICE|AMOUNT|ITEM|PAGE|BID|"
                          r"ESTIMATE|TABULATION|SPEC|UNIT)\b", re.I)


def _attach_wrapped_descriptions(rows, loose):
    """Descriptions often wrap onto lines above/below the priced line. Give each
    loose text line (no prices, no header words) to the nearest row on its page."""
    for ln in loose:
        cands = [r for r in rows if r.page == ln.page and abs(r.y - ln.y) < 2.6 * r.height]
        if not cands:
            continue
        r = min(cands, key=lambda c: abs(c.y - ln.y))
        words = [w[4] for w in ln.words if w[2] < r.qty_x and not re.fullmatch(r"\d{1,3}\.?", w[4])
                 and w[4].upper().strip(".") not in ("LS", "L.S", "EA", "LF", "SY", "SF", "CY", "TON")]
        if not words:
            continue
        text = " ".join(words)
        r.wrap = getattr(r, "wrap", [])
        r.wrap.append((ln.y, text))


def _good(r) -> bool:
    return bool(r) and r.n_ok >= 1 and r.n_ok >= len(r.pairs) - 1


def is_total(ln, r=None) -> bool:
    """A TOTAL/SUBTOTAL line, as opposed to an item that mentions 'total' in its
    description (e.g. 'MOBILIZATION (MAX 10% OF TOTAL BID)'), which has an item code."""
    if not re.search(r"TOTAL", ln.text, re.I):
        return False
    r = r if r is not None else parse_line(ln)
    if r and r.item:          # items carry a code or a line number ('1.04', 'C-105')
        return False
    return not re.search(r"^\W*(\d{1,3}(\.\d{1,3})?|[A-Z]{1,4}-\d)", ln.text) or bool(re.search(r"^\W*(SUB)?TOTAL", ln.text, re.I))


def _money(ln) -> int:
    return len(re.findall(r"\d\.\d\d\b", ln.text))


def merge_split_rows(lines):
    """Rows whose cells are vertically centred split into 2-3 text lines: item and
    quantity on one, some prices on another. Join a non-parsing money line with its
    neighbours when the joined line parses better."""
    parsed = [parse_line(l) for l in lines]
    used = set()
    out = []
    for i, ln in enumerate(lines):
        if i in used:
            continue
        r = parsed[i]
        if _good(r) or not _money(ln) or is_total(ln, r):
            out.append(ln)
            continue
        best = None
        for combo in ((i - 1, i), (i, i + 1), (i - 1, i, i + 1), (i, i + 1, i + 2), (i - 2, i - 1, i)):
            if any(j < 0 or j >= len(lines) or (j != i and (j in used or _good(parsed[j])))
                   or lines[j].page != ln.page or is_total(lines[j], parsed[j]) for j in combo):
                continue
            ys = [lines[j].y for j in combo]
            hgt = max(10, (r.height if r else 30))
            if max(ys) - min(ys) > 3.2 * hgt:
                continue
            words = sorted((w for j in combo for w in lines[j].words), key=lambda w: w[0])
            m = Line(ln.page, statistics.mean(ys), words)
            mr = parse_line(m)
            if mr and mr.n_ok >= 1 and (best is None or mr.n_ok > best[0]):
                best = (mr.n_ok, combo, m)
        if best and best[0] > (r.n_ok if r else 0):
            _, combo, m = best
            if combo[0] < i:          # an earlier line was already emitted: replace it
                for j in combo:
                    if j < i and lines[j] in out:
                        out.remove(lines[j])
            used.update(combo)
            out.append(m)
        else:
            out.append(ln)
    return out


def build(stem: str, cols: list[str], pages, sched_re: str, min_ok: int,
          sections: list[str] | None = None, section_end_re: str | None = None, words_dir: Path = WORDS):
    lines = merge_split_rows(load_lines(words_dir / stem, pages))
    schedule = sections[0] if sections else ""
    sec_i = 0
    rows, totals, loose, missed = [], [], [], []
    for ln in lines:
        r = parse_line(ln)
        up = ln.text.upper()
        if not sections and (r is None or r.n_ok == 0) and re.search(sched_re, up, re.I) and "TOTAL" not in up:
            m = re.search(sched_re, up, re.I)
            schedule = re.sub(r"\s+", " ", m.group(0)).title()
            continue
        if r and r.n_ok >= min_ok and not is_total(ln, r):
            r.schedule = schedule
            rows.append(r)
            continue
        if "TOTAL" in up and re.search(r"\d", up):
            totals.append((ln.page, schedule, ln.text))
            if sections and re.search(section_end_re, up, re.I):
                sec_i += 1
                schedule = sections[min(sec_i, len(sections) - 1)]
            continue
        if not re.search(r"\$|\d,\d{3}", ln.text) and not HEADER_WORDS.search(ln.text):
            loose.append(ln)
        elif _money(ln) >= 2:
            missed.append(ln)
    # summary lines such as "Schedule 1 - PCC-Base Bid  $548,597.00" have no item code
    rows = [r for r in rows if r.item or not re.match(r"^(schedule|bid schedule|base bid|alt|add)", r.description, re.I)]
    _attach_wrapped_descriptions(rows, loose)
    for r in rows:
        if getattr(r, "wrap", None):
            above = [t for y, t in sorted(r.wrap) if y < r.y]
            below = [t for y, t in sorted(r.wrap) if y > r.y]
            r.description = " ".join(above + [r.description] + below).strip()
        # OCR reads a leading 'S' as '$' in codes like 'S1-A2-12'
        m0 = re.match(r"^[\$S]([0-9]-[A-Z0-9]+-\d+)\s+(.*)$", r.description)
        if not r.item and m0:
            r.item, r.description = "S" + m0.group(1), m0.group(2)
        if r.item.startswith("$"):
            r.item = "S" + r.item[1:]
        # "S-1-2.2 Temporary..." when a line number came first: pull the spec code out
        lead = re.match(r"^[\W_]*([\$S]\d{1,2}(?:-[A-Z]{1,2}\d{0,2})?-\d{1,3}|[A-Z]{0,2}\d{1,3}(?:\.\d{1,3})?)[.\])|]?\s+(?=[A-Za-z])",
                        r.description)
        desc = r.description[lead.end():] if lead else r.description            # leading line number
        if lead and not r.item:
            r.item = re.sub(r"^\$", "S", lead.group(1))
        if lead:
            r.description = desc
        m = re.match(r"^[\W_]*(?:[I\[{|]?tem\s+)?([A-Za-z]{1,5}-?\s?\d{1,4}(?:[-.][\w.]+)*)[,._;:]*\s+(.*)$", desc)
        line_no = (not r.item or re.fullmatch(r"[A-Z]{0,3}\d{0,2}[-.]?\s?\d{1,3}(\.\d{1,3})?\.?|[A-Z]{1,3}\d?-[A-Z]?\d+(-\d+)?",
                                              r.item, re.I))
        if line_no and m and re.search(r"[A-Za-z]", m.group(1)) and re.search(r"\d{2,}", m.group(1)) \
                and not re.fullmatch(r"(?i)[A-Z]{1,2}\d?-?\d?\.\d+", m.group(1)):
            r.item, r.description = m.group(1).upper().replace(" ", "").rstrip("._,;:"), m.group(2)
        r.description = re.sub(r"\s+", " ", r.description.replace("|", " ")).strip(" ,;")
    n = len(cols)
    full = [r for r in rows if len(r.pairs) == n and all(p[2] for p in r.pairs)]
    if full:
        xs = [statistics.median(r.pairs[k][3] for r in full) for k in range(n)]
    else:
        xs = None
    items, flags = [], []
    for r in rows:
        prices = [None] * n
        if len(r.pairs) == n or xs is None:
            for k, p in enumerate(r.pairs[:n]):
                prices[k] = p[0]
            bad = [k for k, p in enumerate(r.pairs[:n]) if not p[2]]
        else:
            bad = []
            for p in r.pairs:
                k = min(range(n), key=lambda c: abs(xs[c] - p[3]))
                prices[k] = p[0]
                if not p[2]:
                    bad.append(k)
        items.append({"schedule": r.schedule, "item": r.item, "description": r.description,
                      "unit": r.unit or "", "qty": r.qty, "prices": prices,
                      "_check": [k for k in bad], "_page": r.page, "_y": r.y})
        if bad or len(r.pairs) != n or not r.unit or not r.item:
            flags.append(r)
    build.missed = missed
    return items, totals, flags


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stem")
    ap.add_argument("--cols", required=True)
    ap.add_argument("--pages")
    ap.add_argument("--schedule-re", default=DEFAULT_SCHED)
    ap.add_argument("--min-ok", type=int, default=1)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    cols = a.cols.split("|")
    items, totals, flags = build(a.stem, cols, _pages(a.pages), a.schedule_re, a.min_ok)
    sums = {}
    for it in items:
        for k, p in enumerate(it["prices"]):
            if p is not None:
                key = (it["schedule"], cols[k])
                sums[key] = sums.get(key, 0) + round(p * it["qty"], 2)
    for it in items:
        mark = " CHECK" + str(it["_check"]) if it["_check"] else ""
        print(f"[{it['schedule'][:18]:<18}] {it['item']:<14} {it['unit']:<5} {it['qty']:>10,.2f} "
              f"{it['description'][:38]:<38} {it['prices']}{mark}")
    print("\n-- computed schedule totals")
    for (s, c), v in sums.items():
        print(f"   {s[:24]:<24} {c[:34]:<34} {v:>14,.2f}")
    print("-- TOTAL lines on the tab")
    for pg, s, t in totals:
        print(f"   p{pg} [{s[:18]}] {t[:200]}")
    if a.out:
        a.out.write_text(json.dumps(items, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
