"""TxDOT Aviation bid-tab pipeline.

scrape bid results page -> download project PDF -> extract + verify bids
-> bid summary (Excel + PDF) -> upsert into the master unit-price workbook.
"""

import argparse
import logging
import sys
from pathlib import Path

from txdot_bid_tabs import extractor, master_sheet, reports, scraper
from txdot_bid_tabs.config import HEADLESS, NEW_FORMAT_START
from txdot_bid_tabs.models import ProjectListing

log = logging.getLogger("txdot")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default="Lockhart",
                   help="substring of the project title to process (default: Lockhart)")
    p.add_argument("--all-new", action="store_true",
                   help="process every project in the new (Oct 2026+) format")
    p.add_argument("--full-path", action="store_true",
                   help="click through from the Aviation Division page instead of "
                        "opening Bid Results directly")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--force", action="store_true", help="re-download PDFs even if cached")
    p.add_argument("--pdf", type=Path,
                   help="offline mode: process a local bid-tab PDF, skipping the website")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def process(pdf_path: Path, listing: ProjectListing | None) -> bool:
    """Run extraction + outputs for one PDF. Returns True if the data verified cleanly."""
    project = extractor.extract_project(pdf_path, listing)
    problems = extractor.validate(project)
    for note in project.notes:
        log.info("  note: %s", note)
    for problem in problems:
        log.warning("  PROBLEM: %s", problem)

    url = listing.pdf_url if listing else ""
    xlsx = reports.write_summary_excel(project, source_url=url)
    pdf = reports.write_summary_pdf(project, source_url=url)
    master = master_sheet.upsert_project(project, source_url=url)
    low = project.low_bid
    log.info("  %d bidders, %d items; low bidder %s at $%s",
             len(project.bids), len(project.items), low.bidder, f"{low.base_bid:,.2f}")
    log.info("  wrote %s, %s; updated %s", xlsx.name, pdf.name, master.name)
    return not problems


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    if args.pdf:
        log.info("Offline mode: %s", args.pdf)
        return 0 if process(args.pdf, None) else 2

    listings = scraper.list_projects(full_path=args.full_path,
                                     headless=HEADLESS and not args.headed)
    listings = [l for l in listings if l.opening.date() >= NEW_FORMAT_START]
    if not args.all_new:
        listings = [l for l in listings if args.project.lower() in l.title.lower()]
    if not listings:
        log.error("No matching new-format (Oct 2026+) projects found on the bid results page.")
        return 1

    ok = True
    for listing in listings:
        log.info("Processing %s (%s, opened %s)", listing.title, listing.project_id,
                 f"{listing.opening:%m/%d/%Y}")
        try:
            pdf_path = scraper.download_pdf(listing, force=args.force)
            ok &= process(pdf_path, listing)
        except Exception as exc:  # one bad project shouldn't stop the batch
            log.error("  FAILED %s: %s", listing.project_id, exc, exc_info=args.verbose)
            ok = False

    log.info("Done.")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
