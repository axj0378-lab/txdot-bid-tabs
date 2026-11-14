"""Stage 1 for the legacy (pre-Oct 2026) bid tabs: get every word on every page
with its position, cached as JSON.

Most legacy PDFs have a text layer that is garbage (fonts with no character
map), so the text layer is only trusted when it contains real words.
Otherwise the page is rendered at 300 DPI, turned upright (tesseract OSD) and
OCR'd. Output: <out>/<pdf stem>/p<NN>.json  = {"w":..,"h":..,"source":..,"words":[[x0,y0,x1,y1,text,conf],..]}
Scanned / garbled pages: rendered at 450 DPI, ruling lines erased, then OCR'd.

usage: python scripts/legacy_words.py samples/legacy out/legacy_words [--only STEM]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import pdfplumber
import pypdfium2 as pdfium
import pytesseract

TESS_LINES = False   # --tess-lines: keep tesseract's own line grouping
DPI = 450   # small table fonts need more than 300 (--dpi 600 for the densest tabs)
KEYWORDS = re.compile(r"\b(total|unit|item|bid|price|quantity|description|lump|sum|each)\b", re.I)


def text_layer_ok(pdf: Path) -> bool:
    with pdfplumber.open(pdf) as doc:
        text = " ".join((p.extract_text() or "") for p in doc.pages[:3])
    return len(KEYWORDS.findall(text)) >= 5 and "ÿ" not in text


def words_from_text_layer(pdf: Path, out: Path, xtol: float = 3) -> None:
    scale = DPI / 72
    with pdfplumber.open(pdf) as doc:
        for i, page in enumerate(doc.pages, start=1):
            ws = page.extract_words(keep_blank_chars=False, use_text_flow=False, x_tolerance=xtol, y_tolerance=1)
            words = [[round(w["x0"] * scale), round(w["top"] * scale), round(w["x1"] * scale),
                      round(w["bottom"] * scale), w["text"], 100] for w in ws]
            (out / f"p{i:02d}.json").write_text(json.dumps(
                {"w": round(page.width * scale), "h": round(page.height * scale),
                 "source": "text", "rotate": 0, "words": words}))


def remove_rules(gray: np.ndarray) -> np.ndarray:
    """Erase table ruling lines; OCR reads cells far better without them."""
    bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    k = DPI // 4
    h = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
    v = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, k)))
    out = gray.copy()
    out[cv2.dilate(h | v, np.ones((3, 3), np.uint8)) > 0] = 255
    return out


def ocr_page(args) -> str:
    pdf, i, out, dpi, tess_lines = args
    global DPI, TESS_LINES
    DPI, TESS_LINES = dpi, tess_lines
    doc = pdfium.PdfDocument(str(pdf))
    img = doc[i].render(scale=DPI / 72).to_pil().convert("L")
    rotate = 0
    try:
        osd = pytesseract.image_to_osd(img.reduce(2), config="--psm 0 -c min_characters_to_try=50")
        rotate = int(re.search(r"Rotate: (\d+)", osd).group(1))
    except Exception:
        pass
    if rotate:
        img = img.rotate(-rotate, expand=True)
    img = remove_rules(np.array(img))
    d = pytesseract.image_to_data(img, config="--psm 6", output_type=pytesseract.Output.DICT)
    words = [[d["left"][k], d["top"][k], d["left"][k] + d["width"][k], d["top"][k] + d["height"][k],
              d["text"][k], float(d["conf"][k])]
             for k in range(len(d["text"])) if d["text"][k].strip()]
    if TESS_LINES:   # snap each word to its tesseract line so tightly spaced rows stay apart
        keys = [(d["block_num"][k], d["par_num"][k], d["line_num"][k])
                for k in range(len(d["text"])) if d["text"][k].strip()]
        ys = {}
        for key, w in zip(keys, words):
            ys.setdefault(key, []).append((w[1] + w[3]) / 2)
        mid = {key: sum(v) / len(v) for key, v in ys.items()}
        for key, w in zip(keys, words):
            h = max(4, (w[3] - w[1]) // 4)
            w[1], w[3] = int(mid[key] - h), int(mid[key] + h)
    (out / f"p{i + 1:02d}.json").write_text(json.dumps(
        {"w": img.shape[1], "h": img.shape[0], "source": "ocr", "rotate": rotate, "words": words}))
    return f"{pdf.stem} p{i + 1} rot={rotate} words={len(words)}"


def main() -> int:
    global DPI
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--only")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--pages", help="for --only: comma list of pages")
    ap.add_argument("--force-ocr", action="store_true", help="OCR even when the text layer is usable")
    ap.add_argument("--xtol", type=float, default=3, help="text-layer letter gap that still joins a word")
    ap.add_argument("--dpi", type=int, default=DPI)
    ap.add_argument("--tess-lines", action="store_true")
    a = ap.parse_args()
    DPI = a.dpi
    jobs = []
    for pdf in sorted(a.src.glob("*.pdf")):
        if a.only and a.only not in pdf.stem:
            continue
        out = a.out / pdf.stem
        out.mkdir(parents=True, exist_ok=True)
        if not a.force_ocr and text_layer_ok(pdf):
            words_from_text_layer(pdf, out, a.xtol)
            print(f"{pdf.stem}: text layer", flush=True)
            continue
        n = len(pdfium.PdfDocument(str(pdf)))
        want = {int(x) - 1 for x in a.pages.split(",")} if a.pages else set(range(n))
        jobs += [(pdf, i, out, a.dpi, a.tess_lines) for i in sorted(want) if not (out / f"p{i + 1:02d}.json").exists()]
    with ProcessPoolExecutor(a.jobs) as ex:
        for msg in ex.map(ocr_page, jobs):
            print(msg, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
