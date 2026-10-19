# TxDOT Aviation Bid Tab Extractor

Automates pulling TxDOT Aviation Division bid results and turning them into a
bid comparison and a statewide unit-price database:

1. **Navigates** TxDOT's site with Playwright to the Aviation bid results page
2. **Downloads** the project's bid-tab PDF
3. **Extracts** every bidder's unit prices: typed form fields + OCR of the scanned bid tabs, with cross-checks
4. **Bid summary (Excel):** side-by-side comparison of all bidders, low bidder highlighted
5. **Bid summary (PDF):** the same comparison as a one-page PDF for TxDOT
6. **Master sheet (Excel):** statewide unit-price database, seeded with the Lockhart project

## Deliverables (in `output/`)

| File | What it is |
|---|---|
| `NTB2614LHART00034_bid_summary.xlsx` | Bid tabulation for Lockhart Municipal Airport, plus an *Extraction Log* sheet showing what was verified |
| `NTB2614LHART00034_bid_summary.pdf` | The same tabulation as a PDF |
| `master_unit_prices.xlsx` | Master workbook: *Master Unit Prices* view, *Projects*, and *Bid Data* (the database) |

**Lockhart result:** 3 bidders, 11 items. Low bidder is **Delta Specialty Contractors LLC at
$686,695.00**, followed by Fencing Unlimited LLC ($892,450.00, +30.0%) and Digg Commercial LLC
($1,663,920.00, +142.3%). All three bidders' extracted line items reconcile exactly to the base
bids on TxDOT's summary sheet.

## Setup

Requires **Python 3.10+** and the **Tesseract OCR engine** (a separate program, not a pip package).

1. Install Tesseract:
   - Windows: install from the [UB Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki).
     The script finds the default `C:\Program Files\Tesseract-OCR\tesseract.exe` automatically.
     For any other location, set the `TESSERACT_CMD` environment variable to the exe path.
   - macOS: `brew install tesseract`
   - Ubuntu/Debian: `sudo apt install tesseract-ocr`
2. Install the Python packages and the browser Playwright drives:

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
python main.py                    # go to TxDOT, find Lockhart, download, process
python main.py --full-path        # same, but click through from the Aviation Division page
python main.py --all-new          # process every project in the new (Oct 2026+) format
python main.py --project Zapata   # process a different project by title
python main.py --headed           # watch the browser while it runs
python main.py --force            # re-download PDFs even if already cached in downloads/

python main.py --pdf samples/ntb2614lhart00034br.pdf   # offline: skip the website
python -m pytest -q                                     # run the tests
```

Each run takes about 30-40 seconds per project, most of it OCR. Re-running is safe: the
master sheet replaces a project's rows instead of duplicating them. The exit code is 0 when
everything verified, 2 when something needs review, and 1 when no project matched.

## How it works

```
main.py                    pipeline + command-line options
txdot_bid_tabs/
  config.py                URLs, paths, settings
  models.py                data classes passed between stages
  scraper.py               Playwright navigation, table parsing, PDF download (with retries)
  extractor.py             PDF parsing, cross-checks, auto-correction
  ocr.py                   page rendering, table grid detection, per-cell OCR
  reports.py               bid summary Excel + PDF
  master_sheet.py          master workbook (upsert)
samples/                   the Lockhart PDF, so the pipeline can be run offline
tests/                     unit tests (parsing, reconciliation, upsert)
```

**Navigation.** By default the script opens the Bid Results page directly, as slide 6 suggests.
`--full-path` follows the deck's route instead: Aviation Division page, then *Aviation Resources
and Opportunities*, then *Bid results*. The bid results table is parsed into
(title, project ID, opening date, PDF link) rows. Only projects opened on or after
October 2026 are processed, since TxDOT changed the PDF format that month.

**The PDF is two different kinds of document.** Inspecting the Lockhart PDF showed:

| Pages | Content | How it is read |
|---|---|---|
| 1 | Bid Result Summary Sheet | A **fillable PDF form**. Airport, CSJ, scope, date, bidder names and base bids are read straight from the form fields, exactly, with no OCR. |
| 2, 4, 6 | Bid form page 1 (per bidder) | **Scanned image.** OCR finds "Name of Bidder". |
| 3, 5, 7 | Bid form page 2 (per bidder) | **Scanned image.** OCR reads the bid tab table. |

Full-page OCR mangled the bid tab tables, so `ocr.py` finds the table's ruling lines with
OpenCV and OCRs each cell separately. Numeric columns are restricted to `0-9 $ , .`.

**OCR is never trusted on its own.** Every bidder's numbers are cross-checked:

- **qty x unit price = line total** for every line. When they disagree, one of the two was
  misread. Both readings are tried, and the one whose line totals add up to the base bid wins.
  For example, an early prototype read Digg's `$9,600.00` as `$9,660.00`; 3 x $3,200 = $9,600
  exposes that.
- **Line totals must sum to the base bid** typed on the summary sheet. That number comes from a
  form field, so it is reliable ground truth.
- **Item codes and quantities must match across bidders.** Everyone bids the same items, so a
  majority vote fixes a single bad read. On Lockhart, one blank quantity was filled this way.
- **OCR'd bidder names are matched to the typed names** on the summary sheet, which are used
  as the official names.

Corrections are written to the *Extraction Log* sheet. A bidder that cannot be verified is
still output but flagged in the log, in the summary notes, and in the exit code.

**Master workbook.** *Bid Data* is the database: one row per bidder x item, for **all** bidders,
with the low bid flagged. *Master Unit Prices* is the view from slides 12-13: rows are bid items
and each project adds a Unit Price / Qty block with its project info above it. Its cells are
`SUMIFS` formulas over *Bid Data*, so the view stays consistent with the data. It also shows a
live **Price Age** (last quarter / 6 months / last year / older, based on `TODAY()`) and the
average low-bid unit price per item over those windows, a first step toward slide 14's estimates.

## Assumptions

- **Starting point.** Navigation starts at TxDOT's site, not a Google search (slide 1). Search
  result pages change constantly and block automated browsers. `--full-path` covers the rest
  of the deck's click path.
- **New format only.** Only projects opened on or after 10/1/2026 are processed (configurable as
  `NEW_FORMAT_START` in `config.py`). Older projects use a different PDF layout.
- **PDF structure.** The summary sheet is page 1, and each bidder's section has a "Name of
  Bidder" page followed by a bid tab page. Bidders appear in bid-opening order, not price order.
  Extra pages (e.g. pages 3-4 of the bid form, if TxDOT includes them) are ignored.
- **Low bidder = lowest base bid** on the summary sheet. The PDFs show no additive alternates,
  so qualification or responsiveness reviews are not considered.
- **Bidder ordering.** The summary orders bidders low to high so the low bidder is the first
  block (slide 11's goal). Rank is also shown.
- **Item identity across projects.** Items are matched by **item code + unit** (e.g. `F-164.2`, LF).
  These are standard TxDOT/FAA spec item numbers. The description shown is from the most recent
  project using that item, since wording can vary between projects.
- **Unit prices are taken as bid**, with no inflation adjustment yet (see future work).
- The project ID (e.g. `NTB2614LHART00034`) is the unique key for upserts. In offline mode it is
  taken from TxDOT's file name.

## Limitations and future work

- **Live-site testing.** My development environment could not reach txdot.gov, so the scraper
  was written against the page structure (confirmed from the live page's content and the
  deck's screenshots) and tested against a local copy of that structure. The extraction,
  reports and master sheet were tested end-to-end on the real Lockhart PDF. Run
  `python main.py --headed` to watch the live run.
- **Only one PDF in the new format has been seen.** The table detection reads column headers
  rather than assuming positions, but a different scan quality or layout could need tuning.
  Zapata County Airport (10/14/2026) has since been posted and would be the next test.
- **Inflation adjustment** to today's dollars (slide 12): needs a cost index choice (e.g.
  FHWA NHCCI or ENR CCI) applied by bid date.
- **Regionalization** (slide 7): the *Region* column exists in *Projects* (yellow, user-editable,
  preserved across runs). Assigning regions automatically needs a region definition (e.g. TxDOT
  districts) plus a city/county-to-region lookup from the airport name.
- **Estimates** (slide 14): regional and similar-quantity estimates would build on *Bid Data*.
  It already stores all bidders, so all-bid analysis is possible later.
- **Emailing TxDOT** (slide 11): add an SMTP step that sends the summary Excel and PDF from a
  no-reply account once TxDOT provides recipients. Not built here because it needs credentials.
- **Scheduling:** a daily scheduled run (Windows Task Scheduler / cron) with `--all-new` would
  pick up new projects automatically, since processed projects are cached and upserted.
- **Scale:** the Excel database is fine for hundreds of projects. Beyond that, moving *Bid Data*
  to SQLite or Postgres and generating the Excel views from it would be the natural next step.
