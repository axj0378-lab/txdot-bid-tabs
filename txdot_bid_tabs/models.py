"""Data structures passed between pipeline stages."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ProjectListing:
    """One row of the TxDOT bid results table."""

    title: str               # e.g. "Lockhart Municipal Airport"
    project_id: str          # e.g. "NTB2614LHART00034"
    opening: datetime        # bid opening date and time
    pdf_url: str


@dataclass
class BidItem:
    """A line item every bidder prices (taken from the bid tabs)."""

    item: str                # e.g. "F-164.2"
    description: str
    unit: str                # LS, LF, EA, ...
    qty: float
    schedule: str = ""       # legacy tabs: "Base Bid", "Add Alt 1", "Schedule 2"...
    line: int = 0            # legacy tabs: row position (codes can repeat within a schedule)

    @property
    def key(self) -> str:
        """Unique within a project; legacy tabs repeat item codes across and within schedules."""
        if self.line:
            return f"{self.schedule}|{self.item}|{self.line}"
        return f"{self.schedule}|{self.item}" if self.schedule else self.item


@dataclass
class Bid:
    """One bidder's prices for a project, in bid-opening order."""

    bidder: str
    base_bid: float                                              # from the summary form (typed, authoritative)
    unit_prices: dict[str, float] = field(default_factory=dict)  # item code -> unit price
    totals: dict[str, float] = field(default_factory=dict)       # item code -> extended total
    verified: bool = False                                       # line totals reconcile to base_bid


@dataclass
class Project:
    """Everything extracted from one project PDF."""

    project_id: str          # e.g. "NTB2614LHART00034" (falls back to CSJ if unknown)
    airport: str
    csj: str
    scope: str
    description: str         # long description from the bid form, e.g. "WILDLIFE FENCING AND GATES"
    bid_date: datetime
    location: str = ""
    items: list[BidItem] = field(default_factory=list)
    bids: list[Bid] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # extraction warnings / corrections, for the audit log
    estimate: Bid | None = None   # engineer's estimate, when the tab shows one (never ranked)
    source: str = ""              # "new form" or "legacy tab"

    @property
    def low_bid(self) -> Bid:
        return min(self.bids, key=lambda b: b.base_bid)

    @property
    def label(self) -> str:
        """Column header used in the master sheet, e.g. 'Lockhart Municipal (October 2026)'."""
        name = self.airport.replace(" Airport", "").strip()
        return f"{name} ({self.bid_date:%B %Y})"
