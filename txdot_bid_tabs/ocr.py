"""OCR helpers for the scanned bid-form pages.

The bid tabs are scans, and whole-page OCR mangles the table borders. Instead we:
  1. render the page at 300 DPI and straighten it (scans come in a few tenths
     of a degree off, enough to push ruling lines into the cells),
  2. find the table's ruling lines with morphological filters (OpenCV),
  3. crop every cell and OCR it on its own, with a character whitelist for
     numeric columns so "$150,000.00" can't come back as "$150,000.0€".
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium
import pytesseract

RENDER_DPI = 300

# Tesseract is a system binary, not a pip package. On Windows the installer
# doesn't add it to PATH, so look in the default install locations too
# (all users, and a per-user install without admin rights).
_WINDOWS_DEFAULTS = [Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
                     Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Tesseract-OCR/tesseract.exe"]
if os.environ.get("TESSERACT_CMD"):
    pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]
elif not shutil.which("tesseract"):
    for _exe in _WINDOWS_DEFAULTS:
        if _exe.exists():
            pytesseract.pytesseract.tesseract_cmd = str(_exe)
            break

NUMERIC = "--psm 7 -c tessedit_char_whitelist=0123456789$,."
UNIT = "--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ITEM = "--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-.,"
TEXT = "--psm 6"


def render_pages(pdf_path: Path, dpi: int = RENDER_DPI) -> list[np.ndarray]:
    """Render every page of the PDF to a deskewed grayscale numpy array."""
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        pages = []
        for page in pdf:
            pil = page.render(scale=dpi / 72).to_pil().convert("L")
            pages.append(deskew(np.array(pil)))
        return pages
    finally:
        pdf.close()


def _horizontal_lines(gray: np.ndarray) -> np.ndarray:
    """Mask of long horizontal strokes (table rules)."""
    w = gray.shape[1]
    binary = cv2.adaptiveThreshold(~gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                   cv2.THRESH_BINARY, 15, -2)
    return cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (w // 10, 1)))


def skew_angle(gray: np.ndarray) -> float:
    """Median angle (degrees) of the page's long horizontal lines; 0 if none."""
    w = gray.shape[1]
    contours, _ = cv2.findContours(_horizontal_lines(gray), cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_NONE)
    angles = []
    for c in contours:
        if cv2.boundingRect(c)[2] < w // 4:
            continue  # only long rules give a reliable angle
        vx, vy, _, _ = cv2.fitLine(c, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
        angles.append(np.degrees(np.arctan2(vy, vx)))
    angles = [a for a in angles if abs(a) < 3]
    return float(np.median(angles)) if angles else 0.0


def deskew(gray: np.ndarray) -> np.ndarray:
    """Rotate the page so its table rules are level."""
    angle = skew_angle(gray)
    if abs(angle) < 0.05:
        return gray
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


def page_text(gray: np.ndarray) -> str:
    """Plain whole-page OCR, used for the bid form page (no table)."""
    return pytesseract.image_to_string(gray, config=TEXT)


@dataclass
class TableGrid:
    rows: list[tuple[int, int]]   # (y0, y1) per row, header first
    cols: list[tuple[int, int]]   # (x0, x1) per column


def _line_positions(mask: np.ndarray, axis: int, min_frac: float = 0.5) -> list[int]:
    """Collapse a line mask into the center coordinate of each ruling line."""
    profile = mask.sum(axis=axis) / 255
    if profile.max() == 0:
        return []
    hits = np.where(profile > profile.max() * min_frac)[0]
    groups: list[list[int]] = []
    for i in hits:
        if groups and i - groups[-1][-1] <= 5:
            groups[-1].append(int(i))
        else:
            groups.append([int(i)])
    return [int(np.mean(g)) for g in groups]


def find_table(gray: np.ndarray) -> TableGrid | None:
    """Locate the largest ruled table on the page and return its cell grid."""
    h, w = gray.shape
    binary = cv2.adaptiveThreshold(~gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                   cv2.THRESH_BINARY, 15, -2)
    horiz = _horizontal_lines(gray)
    vert = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (1, h // 50)))
    grid = cv2.dilate(horiz | vert, np.ones((3, 3), np.uint8))

    contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    x, y, bw, bh = cv2.boundingRect(max(contours, key=cv2.contourArea))
    if bw < w * 0.5 or bh < h * 0.05:
        return None  # nothing table-sized on this page

    ys = _line_positions(horiz[y:y + bh + 1, x:x + bw + 1], axis=1)
    xs = _line_positions(vert[y:y + bh + 1, x:x + bw + 1], axis=0)
    if len(ys) < 3 or len(xs) < 3:
        return None
    rows = [(y + a, y + b) for a, b in zip(ys, ys[1:]) if b - a > 20]
    cols = [(x + a, x + b) for a, b in zip(xs, xs[1:]) if b - a > 20]
    return TableGrid(rows=rows, cols=cols)


def ocr_cell(gray: np.ndarray, box: tuple[int, int, int, int], config: str) -> str:
    """OCR one cell. box = (x0, y0, x1, y1); trims the borders and pads with white."""
    x0, y0, x1, y1 = box
    inset = 8
    crop = gray[y0 + inset:y1 - inset, x0 + inset:x1 - inset]
    if crop.size == 0:
        return ""
    crop = cv2.copyMakeBorder(crop, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    text = pytesseract.image_to_string(crop, config=config)
    if not text.strip() and "--psm 7" in config:
        # psm 7 (text line) often returns nothing for a lone short token like "1"
        text = pytesseract.image_to_string(crop, config=config.replace("--psm 7", "--psm 8"))
    return re.sub(r"\s+", " ", text).strip()
