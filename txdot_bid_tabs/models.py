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
    """A line item every bidder prices (taken from the first bid tab)."""

    item: str                # e.g. "F-164.2"
    description: str
    unit: str                # LS, LF, EA, ...
    qty: float


@dataclass
class Bid:
    """One bidder's prices for a project."""

    bidder: str
    unit_prices: dict[str, float] = field(default_factory=dict)  # item -> unit price
    totals: dict[str, float] = field(default_factory=dict)       # item -> extended total
    base_bid: float | None = None


@dataclass
class Project:
    """Everything extracted from one project PDF."""

    airport: str
    csj: str
    scope: str
    bid_date: datetime
    items: list[BidItem] = field(default_factory=list)
    bids: list[Bid] = field(default_factory=list)

    @property
    def low_bid(self) -> Bid:
        return min(self.bids, key=lambda b: b.base_bid or float("inf"))
