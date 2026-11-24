"""Work out the baggage tier(s) of a fare and the total price for each tier.

Rules (from the brief):
* Every fare gets a "no bag" price. If we can't tell what bags it includes, it ONLY
  competes in the no-bag tier and is marked "baggage unverified".
* Carry-on / checked prices = fare + bag fee (x2 for a round trip) when the fare
  does not include the bag. A fare is never compared to a tier it doesn't belong to.
"""
from __future__ import annotations

import re

from .models import Baggage, Offer, TierPrice

_ORDINAL = re.compile(r"\b\d+(st|nd|rd|th)\b")
_KG = re.compile(r"\d+(?:[.,]\d+)?\s*(kg|kgs|lb|lbs|cm|in)\b")
_LEADING_COUNT = re.compile(r"^\s*\d+\s+")
_NUMBER = re.compile(r"(\d{1,5}(?:[.,]\d{1,2})?)")


def _amount(s: str) -> float | None:
    s = _ORDINAL.sub(" ", s)
    s = _KG.sub(" ", s)
    s = _LEADING_COUNT.sub(" ", s)
    nums = [float(n.replace(",", ".")) for n in _NUMBER.findall(s)]
    nums = [n for n in nums if n > 0]
    return max(nums) if nums else None   # ranges like "99-187": take the higher (conservative)


def _classify(text: str) -> tuple[str, float | None]:
    t = text.lower()
    if "not allowed" in t or "no carry-on allowed" in t:
        return "not_allowed", None
    negative = any(w in t for w in ("not included", "no checked", "no carry", "for a fee", "fee"))
    if not negative and ("free" in t or "included" in t or "includes" in t):
        return "included", None
    amt = _amount(t)
    if negative or amt is not None:
        return "fee", amt
    return "unknown", None


def parse_baggage_strings(strings: list[str], source: str) -> Baggage:
    """Parse Google strings such as '1 free carry-on', '1st checked bag: 99-187',
    'Checked baggage for a fee', 'Carry-on bag not included'."""
    b = Baggage(source=source)
    for raw in strings or []:
        t = raw.lower()
        if "carry-on" in t or "carry on" in t or "cabin bag" in t or "hand baggage" in t:
            status, amt = _classify(t)
            if status != "unknown":
                b.carry_on, b.carry_on_fee = status, amt
        elif "checked" in t or "hold bag" in t:
            status, amt = _classify(t)
            if status != "unknown":
                b.checked, b.checked_fee = status, amt
    return b


def merge(base: Baggage, better: Baggage) -> Baggage:
    """Fields known in `better` (e.g. booking data) override `base` (e.g. search extensions)."""
    out = Baggage(**base.__dict__)
    if better.carry_on != "unknown":
        out.carry_on, out.carry_on_fee = better.carry_on, better.carry_on_fee
    if better.checked != "unknown":
        out.checked, out.checked_fee = better.checked, better.checked_fee
    if better.known():
        out.source = better.source
    return out


def _table_fee(cfg: dict, airline: str, kind: str) -> float | None:
    table = cfg.get("bag_fees") or {}
    a = (airline or "").lower()
    for name, fees in table.items():
        n = str(name).lower()
        if n and (n in a or a == n) and fees and fees.get(kind) is not None:
            return float(fees[kind])
    return None


def tier_prices(offer: Offer, cfg: dict) -> dict[str, TierPrice]:
    mult = 2 if cfg.get("baggage", {}).get("fee_applies_per_direction", True) else 1
    b = offer.baggage
    out: dict[str, TierPrice] = {}

    unverified = not b.known()
    out["no_bag"] = TierPrice(
        "no_bag", offer.price, 0.0, verified=not unverified,
        note="baggage unverified" if unverified else "personal item only",
    )

    for tier, status, fee, kind in (
        ("carry_on", b.carry_on, b.carry_on_fee, "carry_on"),
        ("checked", b.checked, b.checked_fee, "checked"),
    ):
        label = "carry-on" if kind == "carry_on" else "checked bag"
        if status == "included":
            out[tier] = TierPrice(tier, offer.price, 0.0, True, f"{label} included in fare")
        elif status == "fee" and fee is not None:
            cost = fee * mult
            out[tier] = TierPrice(tier, offer.price + cost, cost, True,
                                  f"fare + {label} fee ${cost:.0f} (from Google booking data)")
        elif status == "not_allowed":
            continue
        else:
            tf = _table_fee(cfg, offer.airline, kind)
            if tf is not None:
                cost = tf * mult
                out[tier] = TierPrice(tier, offer.price + cost, cost, False,
                                      f"fare + est. {label} fee ${cost:.0f} (your bag_fees table)")
    return out
