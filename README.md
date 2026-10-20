# TxDOT Aviation Bid Tab Extractor

Automates pulling TxDOT Aviation Division bid results and turning them into a
bid comparison and a statewide unit-price database:

1. **Navigates** TxDOT's site with Playwright to the Aviation bid results page
2. **Downloads** the project's bid-tab PDF
3. **Extracts** every bidder's unit prices: typed form fields + OCR of the scanned bid tabs, with cross-checks
4. **Bid summary (Excel):** side-by-side comparison of all bidders, low bidder highlighted
5. **Bid summary (PDF):** the same comparison as a PDF for TxDOT
6. **Master sheet (Excel):** statewide unit-price database, seeded with the Lockhart project

## Submission contents

| Requested | File(s) |
|---|---|
| Script/code files | `main.py` and the `txdot_bid_tabs/` package (plus `tests/` and `requirements.txt`) |
| Excel summary | `output/NTB2614LHART00034_bid_summary.xlsx` (Lockhart Municipal Airport), with an *Extraction Log* sheet showing what was verified |
| PDF summary | `output/NTB2614LHART00034_bid_summary.pdf` |
| Master Excel sheet | `output/master_unit_prices.xlsx`: *Master Unit Prices* view, *Projects*, and *Bid Data* (the database) |
| README / notes | This file: [How to run](#how-to-run), [Required packages](#required-packages-and-dependencies), [Assumptions](#assumptions), [Incomplete parts](#incomplete-parts-and-further-development) |

A second project in the new format, Zapata County Airport, was also processed:
`output/NTB2521ZAPAT00053_bid_summary.xlsx` / `.pdf`, and it is included in the master sheet.
`samples/` holds the Lockhart bid-tab PDF so the script can be run without the website.

**Lockhart result:** 3 bidders, 11 items. Low bidder is **Delta Specialty Contractors LLC at
$686,695.00**, followed by Fencing Unlimited LLC ($892,450.00, +30.0%) and Digg Commercial LLC
($1,663,920.00, +142.3%).

**Zapata County result:** 3 bidders, 51 items. Low bidder is **Anderson Columbia Company, Inc. at
$5,660,055.00**, followed by Sterling Delaware Holding Company ($6,945,199.67, +22.7%) and
Foremost Paving, Inc. ($8,185,518.05, +44.6%).

For both projects, every bidder's extracted line items reconcile exactly to the base bids on
TxDOT's summary sheet.

## What's in this folder

```
txdot-bid-tabs/
  README.md                  this file
  requirements.txt           Python packages to install (see below)
  main.py                    the script you run: runs the whole pipeline, start to finish

  txdot_bid_tabs/            the code, one file per step
    config.py                  settings: TxDOT URLs, folder paths, OCR resolution, format cutoff date
    models.py                  data classes passed between steps (project, bidder, line item)
    scraper.py                 step 1-2: opens TxDOT's site, reads the bid results table, downloads the PDF
    ocr.py                     step 3: turns scanned pages into images, straightens them, finds the
                               table grid, reads each cell with Tesseract
    extractor.py               step 3: reads the PDF's form fields, assembles each bidder's line items,
                               and cross-checks/corrects the OCR (see "How it works")
    reports.py                 step 4-5: writes the bid summary Excel and PDF
    master_sheet.py            step 6: adds the project to the master unit-price workbook

  output/                    the results (re-created every time the script runs)
    NTB2614LHART00034_bid_summary.xlsx   Lockhart bid summary (Excel)
    NTB2614LHART00034_bid_summary.pdf    Lockhart bid summary (PDF)
    NTB2521ZAPAT00053_bid_summary.xlsx   Zapata County bid summary (Excel)
    NTB2521ZAPAT00053_bid_summary.pdf    Zapata County bid summary (PDF)
    master_unit_prices.xlsx              master sheet: both projects combined

  samples/
    ntb2614lhart00034br.pdf  TxDOT's original Lockhart bid-tab PDF, for running without the website

  tests/
    test_core.py             automated tests of the parsing, cross-checks and master-sheet logic
```

The output files are named by TxDOT's project ID (`NTB2614LHART00034` = Lockhart).

**Inside the output files**

- **Bid summary Excel**, sheet *Bid Summary*: project info at the top (airport, CSJ, scope, bid
  date), then one row per bid item, with each bidder's unit price and line total side by side.
  Bidders are ordered low to high, the low bidder is highlighted, and the totals row matches
  the base bids. Sheet *Extraction Log*: for each bidder, whether its numbers verified against
  TxDOT's base bid, and any OCR readings that were corrected.
- **Bid summary PDF**: the same tabulation, ready to send (1 page for Lockhart, 4 for Zapata's
  51 items).
- **Master sheet**, three tabs:
  - *Bid Data*: the database. One row per bidder per item, for every project and every
    bidder, with the low bid flagged.
  - *Projects*: one row per project (ID, airport, date, low bidder, number of bidders). Its
    *Region* column is left for the user to fill in and is kept across runs.
  - *Master Unit Prices*: the view. One row per bid item, with a Unit Price / Qty column block
    per project, plus the price age and average low-bid unit price per item. These cells are
    formulas over *Bid Data*, so they update when data is added.

**What happens when you run `python main.py`**

1. `scraper.py` opens TxDOT's Aviation Bid Results page in a hidden Chrome browser and reads
   the table of projects.
2. It keeps projects opened on or after 10/1/2026 (the new PDF format), picks Lockhart (or the
   project you name), and downloads its bid-tab PDF into `downloads/`.
3. `extractor.py` reads page 1's summary sheet (bidder names and base bids, exact form
   fields), then uses `ocr.py` to read each bidder's scanned bid tab, and cross-checks every
   number until each bidder's line items add up to their base bid.
4. `reports.py` writes the bid summary Excel and PDF into `output/`.
5. `master_sheet.py` adds the project to `output/master_unit_prices.xlsx`, replacing its rows if
   the project is already there, so running twice never duplicates data.
6. The console prints the low bidder and the files written. The script exits with 0 if every
   bidder verified, or 2 if anything needs a manual check (also noted in the Extraction Log).

The files in `output/` came from running `python main.py --all-new` on 10/19/2026, which processed
both new-format projects on the site. Running it again overwrites them with the same results.

## Required packages and dependencies

Written in **Python 3.10+**. It also needs the **Tesseract OCR engine**, a separate program
(not a pip package). Python packages (`requirements.txt`):

| Package | Used for |
|---|---|
| `playwright` | Browser automation: navigate TxDOT's site, download the PDFs |
| `pypdf` | Read the fillable form fields on the bid result summary sheet |
| `pypdfium2` | Render the scanned pages to images for OCR |
| `opencv-python-headless`, `numpy` | Straighten pages and find the bid-tab table grid |
| `pytesseract` | Python wrapper for Tesseract |
| `openpyxl` | Excel outputs |
| `reportlab` | PDF output |
| `pytest` | Tests only |

Setup:

1. Install Tesseract:
   - Windows: install from the [UB Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki).
     The script finds the default `C:\Program Files\Tesseract-OCR\tesseract.exe` automatically.
     For any other location (e.g. a per-user install without admin rights:
     `tesseract-ocr-w64-setup-*.exe /S /CurrentUser /D=%LOCALAPPDATA%\Programs\Tesseract-OCR`),
     set the `TESSERACT_CMD` environment variable to the exe path.
   - macOS: `brew install tesseract`
   - Ubuntu/Debian: `sudo apt install tesseract-ocr`
2. Install the Python packages and the browser Playwright drives:

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
playwright install chromium
```

## How to run

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

Each run takes about 1 minute for a small project like Lockhart (7 pages) and about 4 minutes
for Zapata (16 pages, 51 items), almost all of it OCR. Re-running is safe: the
master sheet replaces a project's rows instead of duplicating them. The exit code is 0 when
everything verified, 2 when something needs review, and 1 when no project matched.

## How it works

**Navigation.** By default the script opens the Bid Results page directly, as slide 6 suggests.
`--full-path` follows the deck's route instead: Aviation Division page, then *Aviation Resources
and Opportunities*, then *Bid results*. The bid results table is parsed into
(title, project ID, opening date, PDF link) rows. Only projects opened on or after
October 2026 are processed, since TxDOT changed the PDF format that month.

**The PDF is two different kinds of document.** Page 1 is the Bid Result Summary Sheet, a
**fillable PDF form**: airport, CSJ, scope, date, bidder names and base bids are read straight
from the form fields, exactly, with no OCR. The rest is each bidder's **scanned** bid form, in
bid-opening order. Two layouts of the bid form have been seen:

| | Lockhart (4-page bid form) | Zapata (6-page bid form, Form 2506) |
|---|---|---|
| Pages per bidder in the PDF | 2: name page, then the bid tab | 5: name **and** the first bid tab rows on one page, the table continued over 3 more pages, then a "Bid Summary" page |
| *Item* column | Spec code (`F-164.2`) | Line number (`1`..`51`); the spec code leads the description (`Item P-152-4.1, Drainage ...`) |

The extractor handles both: a page with "Name of Bidder" starts a bidder (the area above a
table is OCR'd too), and every table page after it adds rows to that bidder until the next
name. When the Item column isn't a spec code, the code is taken from the description
(`TxDOT Item 341` becomes `TX-341`, matching the TxDOT codes on Lockhart). Codes are kept as
printed; TxDOT's own forms mix styles (`L-125.1` and `L-125-3` on the same page).

Full-page OCR mangled the bid tab tables, so `ocr.py` straightens each page (scans come in up to
~0.6° off, enough to push ruling lines into the cells), finds the table's ruling lines with
OpenCV and OCRs each cell separately. Numeric columns are restricted to `0-9 $ , .`.

**OCR is never trusted on its own.** Every bidder's numbers are cross-checked:

- **qty x unit price = line total** for every line. When they disagree, one of the two was
  misread. Both readings are tried, and the one whose line totals add up to the base bid wins.
  For example, an early prototype read Digg's `$9,600.00` as `$9,660.00`; 3 x $3,200 = $9,600
  exposes that.
- **Line totals must sum to the base bid** typed on the summary sheet. That number comes from a
  form field, so it is reliable ground truth.
- **Item codes and quantities must match across bidders.** Everyone bids the same items, so a
  vote fixes a single bad read. For quantities the vote prefers the value under which the most
  bidders' qty x unit price = total, so a tie (Zapata's `11` read once as `1`, once not at all)
  is settled by the prices rather than by chance.
- **One line left over is solved from the base bid.** If both numbers on a single line are
  misread (Zapata: $500.50 / $5,506.10 for $500.56 / $5,506.16), the base bid implies that line's
  total. It is accepted only if it gives a whole-cent unit price within 1% of what OCR read, and
  the correction is logged.
- **OCR'd bidder names are matched to the typed names** on the summary sheet, which are used
  as the official names. Matching ignores case and expands abbreviations ("CO., INC." on the bid
  form vs "Company, Inc." on the summary sheet).

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

## Incomplete parts and further development

- **Live-site testing.** The full pipeline was run against the live TxDOT site on 10/19/2026:
  both the direct and `--full-path` navigation, and `--all-new`, which found 18 projects and
  processed the two in the new format (Lockhart and Zapata County) with every bidder verified.
  Run `python main.py --headed` to watch it.
- **Two PDFs in the new format have been seen**, with two different bid form layouts (see above).
  The table detection reads column headers rather than assuming positions, but a new layout or
  much poorer scan could still need tuning. Unverified bidders are flagged, never silently
  accepted, so the exit code and Extraction Log show when that happens.
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
