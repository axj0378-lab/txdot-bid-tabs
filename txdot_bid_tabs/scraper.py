"""Stages 1-2: navigate TxDOT's site with Playwright and download project PDFs.

Two ways in:
  * direct (default): open the Bid Results page.
  * full path (--full-path): start at the Aviation Division page and click
    "Aviation Resources and Opportunities" -> "Bid results", the same route as
    the instructions deck. (The deck's first step, a Google search, is skipped:
    search results pages are unstable and block automated browsers.)

Bid Results page structure (Oct 2026): a table with columns
Title | Project ID | Opening Date & Time, where the PDF link sits on an icon in
the Title cell. The table sits inside a layout table, so rows are identified by
"has a .pdf link and >= 3 cells" rather than by a fixed selector path.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Page, sync_playwright

from .config import (AVIATION_DIVISION_URL, BID_RESULTS_URL, DOWNLOAD_DIR, HEADLESS,
                     NAV_LINK_BID_RESULTS, NAV_LINK_RESOURCES, NAV_TIMEOUT_MS)
from .models import ProjectListing

log = logging.getLogger(__name__)

DATE_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y")


class ScrapeError(RuntimeError):
    pass


def _follow_link(page: Page, text: str) -> None:
    """Click the first visible link whose text matches (case-insensitive)."""
    link = page.locator("a:visible", has_text=re.compile(re.escape(text), re.I)).first
    if link.count() == 0:
        raise ScrapeError(f"link '{text}' not found on {page.url}; has the site changed?")
    href = link.get_attribute("href")
    before = page.url
    link.click()
    page.wait_for_load_state("domcontentloaded")
    if page.url == before and href:  # e.g. link opened a new tab; follow it here instead
        page.goto(urljoin(before, href))
    log.info("clicked '%s' -> %s", text, page.url)


def _parse_rows(page: Page) -> list[ProjectListing]:
    listings = []
    for row in page.locator("tr").all():
        cells = row.locator(":scope > td")  # direct cells only, not the layout table's
        if cells.count() < 3:
            continue
        link = cells.first.locator("a[href$='.pdf' i]")
        if link.count() == 0:
            continue
        title, project_id, opening = (cells.nth(i).inner_text().strip() for i in range(3))
        dt = None
        for fmt in DATE_FORMATS:
            try:
                dt = datetime.strptime(opening, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            log.warning("skipping row with unreadable date: %r", opening)
            continue
        listings.append(ProjectListing(
            title=title,
            project_id=project_id,
            opening=dt,
            pdf_url=urljoin(page.url, link.first.get_attribute("href")),
        ))
    return listings


def list_projects(full_path: bool = False, headless: bool = HEADLESS) -> list[ProjectListing]:
    """Open the bid results page and return every project row in the table."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)
            if full_path:
                page.goto(AVIATION_DIVISION_URL)
                _follow_link(page, NAV_LINK_RESOURCES)
                _follow_link(page, NAV_LINK_BID_RESULTS)
            else:
                page.goto(BID_RESULTS_URL)
            page.wait_for_selector("a[href$='.pdf' i]")
            listings = _parse_rows(page)
        finally:
            browser.close()
    if not listings:
        raise ScrapeError("bid results table found but no project rows parsed")
    log.info("found %d projects on the bid results page", len(listings))
    return listings


def download_pdf(listing: ProjectListing, dest_dir: Path = DOWNLOAD_DIR,
                 force: bool = False, retries: int = 3) -> Path:
    """Download one project's bid-tab PDF and return its local path.

    Uses Playwright's request context (same browser stack, no extra library).
    Skips the download if the file is already there, unless force=True.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(listing.pdf_url).name
    if dest.exists() and not force:
        log.info("using cached %s", dest.name)
        return dest

    with sync_playwright() as p:
        request = p.request.new_context()
        try:
            for attempt in range(1, retries + 1):
                try:
                    resp = request.get(listing.pdf_url, timeout=NAV_TIMEOUT_MS)
                    body = resp.body()
                    if resp.ok and body.startswith(b"%PDF"):
                        dest.write_bytes(body)
                        log.info("downloaded %s (%d KB)", dest.name, len(body) // 1024)
                        return dest
                    log.warning("attempt %d: HTTP %d / not a PDF", attempt, resp.status)
                except Exception as exc:  # network hiccup; retry
                    log.warning("attempt %d failed: %s", attempt, exc)
                time.sleep(2 * attempt)
        finally:
            request.dispose()
    raise ScrapeError(f"could not download {listing.pdf_url} after {retries} attempts")
