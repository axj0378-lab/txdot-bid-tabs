# TxDOT Aviation Bid Tab Extractor

Automates pulling TxDOT Aviation Division bid results, extracting each
bidder's unit prices from the project PDF, and producing:

- a per-project **bid summary** (Excel + PDF) comparing all bidders side by side, and
- a statewide **master unit-price sheet** built from each project's low bid.

> Work in progress. Sections marked TODO are filled in as the pipeline is built.

## Project layout

```
main.py                    entry point / pipeline orchestration
txdot_bid_tabs/
  config.py                URLs, paths, settings
  models.py                data classes passed between stages
  scraper.py               Playwright navigation + PDF download
  extractor.py             PDF parsing + validation
  reports.py               bid summary Excel + PDF
  master_sheet.py          master unit-price sheet (upsert)
downloads/                 downloaded source PDFs (git-ignored)
output/                    generated deliverables
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
python main.py                  # process the Lockhart project
python main.py --all-new        # process every Oct 2026+ project
python main.py --full-path      # click through from the Aviation home page
python main.py --headed         # watch the browser
```

## Assumptions

TODO

## Limitations / future work

TODO
