"""Alternative stage 1 for ruled legacy tabs: OCR cell by cell using the table grid
(txdot_bid_tabs.ocr.find_table / ocr_cell, the same method as the new-format
extractor). Every grid row becomes one text line, so a row's prices can't drift
onto the line above or below. Writes the same word-cache JSON as legacy_words.py.

Tables are found repeatedly per page (the largest one, then masked out, then the
next), so pages with a base bid table and an alternate table both get read.

usage: python scripts/legacy_grid.py samples/legacy/<file>.pdf out/legacy_words [--dpi 400] [--pages 1,2]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
import pytesseract

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from txdot_bid_tabs import ocr  # noqa: E402

CELL = "--psm 6"   # text cells can wrap onto two lines
NUMERIC = "--psm 7 -c tessedit_char_whitelist=0123456789$,.()-"


def _is_numeric_text(t: str) -> bool:
    return bool(re.fullmatch(r"[\s$S§,.\d()\-]+", t)) and any(c.isdigit() for c in t)


def clean_number_cell(text: str) -> str:
    """One number per cell. Drop a leading '$' read as '3'/'5'/'S', and turn all but
    the last '.' into thousands commas ('6.450.00' -> '6,450.00')."""
    toks = [t for t in text.split() if re.search(r"\d", t)]
    if not toks:
        return text
    neg = "(" in text and ")" in text
    t = toks[-1] if len(toks) > 1 and re.search(r"[.,]", toks[-1]) else "".join(toks)
    t = t.strip("$()")
    if t.count(".") > 1:
        head, _, tail = t.rpartition(".")
        t = head.replace(".", ",") + "." + tail
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", t):          # '4.300' is 4,300 (no cents)
        t = t.replace(".", ",")
    return f"({t})" if neg else t


def tables(gray: np.ndarray, max_tables: int = 4):
    work = gray.copy()
    for _ in range(max_tables):
        g = ocr.find_table(work)
        if not g or len(g.rows) < 2:
            return
        yield g
        y0, y1 = g.rows[0][0], g.rows[-1][1]
        x0, x1 = g.cols[0][0], g.cols[-1][1]
        work[max(0, y0 - 6):y1 + 6, max(0, x0 - 6):x1 + 6] = 255


def ocr_page(args) -> str:
    pdf, i, out, dpi = args
    doc = pdfium.PdfDocument(str(pdf))
    gray = ocr.deskew(np.array(doc[i].render(scale=dpi / 72).to_pil().convert("L")))
    words = []
    n_tables = 0
    for g in tables(gray):
        n_tables += 1
        for (y0, y1) in g.rows:
            yc = (y0 + y1) / 2
            for (x0, x1) in g.cols:
                text = ocr.ocr_cell(gray, (x0, y0, x1, y1), CELL)
                if not text:
                    continue
                if _is_numeric_text(text):        # re-read numbers with a digit whitelist
                    t2 = ocr.ocr_cell(gray, (x0, y0, x1, y1), NUMERIC)
                    text = clean_number_cell(t2 or text)
                toks = text.split()
                step = (x1 - x0) / max(len(toks), 1)
                for k, t in enumerate(toks):
                    a = x0 + k * step
                    words.append([int(a), int(yc - 12), int(a + step * 0.9), int(yc + 12), t, 90.0])
    if n_tables == 0:  # no grid: fall back to whole-page OCR with rules erased
        sys.path.insert(0, str(Path(__file__).parent))
        from legacy_words import remove_rules
        d = pytesseract.image_to_data(remove_rules(gray), config="--psm 6", output_type=pytesseract.Output.DICT)
        words = [[d["left"][k], d["top"][k], d["left"][k] + d["width"][k], d["top"][k] + d["height"][k],
                  d["text"][k], float(d["conf"][k])] for k in range(len(d["text"])) if d["text"][k].strip()]
    (out / f"p{i + 1:02d}.json").write_text(json.dumps(
        {"w": gray.shape[1], "h": gray.shape[0], "source": "grid", "rotate": 0, "dpi": dpi, "words": words}))
    return f"{pdf.stem} p{i + 1}: {n_tables} tables, {len(words)} words"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--dpi", type=int, default=400)
    ap.add_argument("--pages")
    ap.add_argument("--jobs", type=int, default=2)
    a = ap.parse_args()
    out = a.out / a.pdf.stem
    out.mkdir(parents=True, exist_ok=True)
    n = len(pdfium.PdfDocument(str(a.pdf)))
    pages = [int(p) - 1 for p in a.pages.split(",")] if a.pages else range(n)
    with ProcessPoolExecutor(a.jobs) as ex:
        for msg in ex.map(ocr_page, [(a.pdf, i, out, a.dpi) for i in pages]):
            print(msg, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
