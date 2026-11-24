"""Google Flights via SerpApi (live prices). Docs: https://serpapi.com/google-flights-api

One search = one date pair, round trip. Returns the outbound options, each priced as a
full round trip. Return-leg details and bag prices need follow-up searches
(departure_token -> booking_token), which we only spend on promising fares.
"""
from __future__ import annotations

import logging
from datetime import date

from ..baggage import merge, parse_baggage_strings
from ..http import Http, HttpError
from ..models import Offer
from . import SourceError

log = logging.getLogger(__name__)

SEARCH_URL = "https://serpapi.com/search.json"
ACCOUNT_URL = "https://serpapi.com/account.json"
NO_RESULTS_HINTS = ("hasn't returned any results", "no results")


class GoogleFlights:
    name = "google_flights"

    def __init__(self, http: Http, cfg: dict, api_key: str):
        self.http = http
        self.cfg = cfg
        self.key = api_key
        self.searches_used = 0

    # ---- helpers -----------------------------------------------------------
    def _base_params(self, out_d: date, ret_d: date) -> dict:
        t = self.cfg["trip"]
        sc = self.cfg["sources"]["serpapi"]
        max_stops = int(t["max_stops_per_direction"])
        stops_param = {0: 1, 1: 2, 2: 3}.get(max_stops, 0)   # SerpApi: 1 nonstop, 2 <=1 stop, 3 <=2 stops
        return {
            "engine": "google_flights",
            "departure_id": t["origin"],
            "arrival_id": t["destination"],
            "outbound_date": out_d.isoformat(),
            "return_date": ret_d.isoformat(),
            "type": 1,
            "adults": int(t.get("adults", 1)),
            "currency": t.get("currency", "USD"),
            "hl": sc.get("hl", "en"),
            "gl": sc.get("gl", "pe"),
            "stops": stops_param,
            "max_duration": int(float(t["max_duration_hours_per_direction"]) * 60),
            "api_key": self.key,
        }

    def _call(self, params: dict) -> dict:
        self.searches_used += 1
        try:
            data = self.http.get_json(SEARCH_URL, params=params, timeout=90)
        except HttpError as e:
            raise SourceError(f"SerpApi request failed: {e}") from e
        err = data.get("error")
        if err:
            if any(h in err.lower() for h in NO_RESULTS_HINTS):
                return {"_empty": True, **data}
            raise SourceError(f"SerpApi error: {self.http.redact(err)}")
        return data

    def _limits_ok(self, stops: int | None, minutes: int | None) -> bool:
        t = self.cfg["trip"]
        if stops is not None and stops > int(t["max_stops_per_direction"]):
            return False
        if minutes is not None and minutes > float(t["max_duration_hours_per_direction"]) * 60:
            return False
        return True

    # ---- public --------------------------------------------------------------
    def searches_left(self) -> int | None:
        """Account API (does not use a search). None if unavailable."""
        try:
            data = self.http.get_json(ACCOUNT_URL, params={"api_key": self.key}, attempts=2, timeout=20)
        except HttpError as e:
            log.warning("SerpApi account check failed: %s", e)
            return None
        for k in ("total_searches_left", "plan_searches_left"):
            if isinstance(data.get(k), int):
                return data[k]
        return None

    def search_pair(self, out_d: date, ret_d: date) -> tuple[list[Offer], dict]:
        data = self._call(self._base_params(out_d, ret_d))
        meta = {
            "url": (data.get("search_metadata") or {}).get("google_flights_url", ""),
            "price_insights": data.get("price_insights") or {},
        }
        offers: list[Offer] = []
        for it in (data.get("best_flights") or []) + (data.get("other_flights") or []):
            price = it.get("price")
            if not isinstance(price, (int, float)):
                continue
            stops = len(it.get("layovers") or [])
            dur = it.get("total_duration")
            if not self._limits_ok(stops, dur):
                continue
            offers.append(Offer(
                source=self.name,
                out_date=out_d, ret_date=ret_d,
                price=float(price),
                airline=_airlines(it),
                stops_out=stops,
                duration_out_min=dur,
                link=meta["url"],
                baggage=parse_baggage_strings(it.get("extensions") or [], "google-extensions"),
                departure_token=it.get("departure_token"),
            ))
        offers.sort(key=lambda o: o.price)
        return offers, meta

    def enrich(self, offer: Offer) -> Offer:
        """Fetch return-leg details (+1 search) and bag prices (+1 search). Best effort."""
        if not offer.departure_token:
            return offer
        params = self._base_params(offer.out_date, offer.ret_date)
        params["departure_token"] = offer.departure_token
        data = self._call(params)
        returns = [r for r in (data.get("best_flights") or []) + (data.get("other_flights") or [])
                   if isinstance(r.get("price"), (int, float))]
        if not returns:
            return offer
        ok = [r for r in returns if self._limits_ok(len(r.get("layovers") or []), r.get("total_duration"))]
        if not ok:
            log.info("All return options for %s break your stop/duration limits: ignoring fare", offer.pair_key)
            offer.excluded = True
            return offer
        ret = min(ok, key=lambda r: r["price"])
        offer.stops_ret = len(ret.get("layovers") or [])
        offer.duration_ret_min = ret.get("total_duration")
        ret_airline = _airlines(ret)
        if ret_airline and ret_airline not in offer.airline:
            offer.airline = f"{offer.airline} / {ret_airline}"
        offer.price = float(ret["price"])   # exact round-trip price for this outbound+return
        offer.baggage = merge(offer.baggage, parse_baggage_strings(ret.get("extensions") or [], "google-extensions"))
        offer.enriched = True

        token = ret.get("booking_token")
        if not token:
            return offer
        t = self.cfg["trip"]
        sc = self.cfg["sources"]["serpapi"]
        b = self._call({"engine": "google_flights", "booking_token": token, "currency": t.get("currency", "USD"),
                        "hl": sc.get("hl", "en"), "gl": sc.get("gl", "pe"), "api_key": self.key})
        url = (b.get("search_metadata") or {}).get("google_flights_url")
        if url:
            offer.link = url
        bag_strings = _booking_bag_strings(b)
        if bag_strings:
            offer.baggage = merge(offer.baggage, parse_baggage_strings(bag_strings, "google-booking"))
        return offer


def _airlines(it: dict) -> str:
    names: list[str] = []
    for seg in it.get("flights") or []:
        a = seg.get("airline")
        if a and a not in names:
            names.append(a)
    return " + ".join(names) if names else "?"


def _booking_bag_strings(b: dict) -> list[str]:
    """Bag strings from the cheapest single-ticket booking option (airline-sold preferred)."""
    best = None
    for opt in b.get("booking_options") or []:
        tog = opt.get("together")
        if not tog or opt.get("separate_tickets"):
            continue
        if not isinstance(tog.get("price"), (int, float)):
            continue
        rank = (0 if tog.get("airline") else 1, tog["price"])
        if best is None or rank < best[0]:
            best = (rank, tog)
    if best and best[1].get("baggage_prices"):
        return list(best[1]["baggage_prices"])
    top = b.get("baggage_prices")
    if isinstance(top, dict) and top.get("together"):
        return list(top["together"])
    return []
