"""Run the full pipeline: scrape -> download -> extract -> reports -> master sheet."""

import argparse
import logging

from txdot_bid_tabs import extractor, master_sheet, reports, scraper
from txdot_bid_tabs.config import NEW_FORMAT_START

log = logging.getLogger("txdot")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", default="Lockhart",
                   help="Substring of the project title to process (default: Lockhart)")
    p.add_argument("--all-new", action="store_true",
                   help="Process every project in the new (Oct 2026+) format")
    p.add_argument("--full-path", action="store_true",
                   help="Click through from the Aviation home page instead of starting at Bid Results")
    p.add_argument("--headed", action="store_true", help="Show the browser window")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    listings = scraper.list_projects(full_path=args.full_path)
    listings = [l for l in listings if l.opening.date() >= NEW_FORMAT_START]
    if not args.all_new:
        listings = [l for l in listings if args.project.lower() in l.title.lower()]
    if not listings:
        raise SystemExit("No matching new-format projects found on the bid results page.")

    for listing in listings:
        log.info("Processing %s (%s)", listing.title, listing.project_id)
        pdf_path = scraper.download_pdf(listing)
        project = extractor.extract_project(pdf_path, listing)
        for problem in extractor.validate(project):
            log.warning("%s: %s", listing.project_id, problem)
        reports.write_summary_excel(project)
        reports.write_summary_pdf(project)
        master_sheet.upsert_project(project)

    log.info("Done.")


if __name__ == "__main__":
    main()
