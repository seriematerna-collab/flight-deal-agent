"""Data classes shared by all modules."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass
class Baggage:
    # status: included | fee | not_allowed | unknown
    carry_on: str = "unknown"
    carry_on_fee: float | None = None   # USD per bag per direction, when known
    checked: str = "unknown"
    checked_fee: float | None = None
    source: str = ""                    # where the info came from: "google-extensions", "google-booking"

    def known(self) -> bool:
        return self.carry_on != "unknown" or self.checked != "unknown"


@dataclass
class Offer:
    source: str                 # "google_flights" | "aviasales_cache"
    out_date: date
    ret_date: date
    price: float                # round-trip fare in USD as found (no bags added)
    airline: str = "?"
    destination: str = ""       # arrival airport, e.g. MAD or BCN
    stops_out: int | None = None
    stops_ret: int | None = None
    duration_out_min: int | None = None
    duration_ret_min: int | None = None
    link: str = ""
    baggage: Baggage = field(default_factory=Baggage)
    cached: bool = False        # True when the price comes from a cache (may be stale)
    departure_token: str | None = None
    enriched: bool = False
    excluded: bool = False      # set when the return leg turns out to break the stops/duration limits

    @property
    def pair_key(self) -> str:
        return f"{self.out_date.isoformat()}|{self.ret_date.isoformat()}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["out_date"] = self.out_date.isoformat()
        d["ret_date"] = self.ret_date.isoformat()
        d.pop("departure_token", None)
        return d


@dataclass
class TierPrice:
    tier: str
    total: float
    bag_cost: float
    verified: bool      # False when baggage info is missing/unclear or estimated
    note: str


@dataclass
class FeedDeal:
    title: str
    link: str
    published: str
    price_usd: float | None
    error_fare: bool
