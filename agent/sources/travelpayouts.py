"""Aviasales cached prices via the Travelpayouts Data API (free).
Docs: https://support.travelpayouts.com/hc/en-us/articles/203956163

These are prices other travellers found in recent searches (cached up to ~7 days),
so they can be stale. Cheap ones are re-checked on Google Flights before alerting.
One request per (departure month x return month) covers every date pair at once.
"""
from __future__ import annotations

import logging
from datetime import date

from ..config import date_window, in_window
from ..http import Http, HttpError
from ..models import Baggage, Offer
from . import AIRLINE_NAMES, SourceError

log = logging.getLogger(__name__)

URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


class AviasalesCache:
    name = "aviasales_cache"

    def __init__(self, http: Http, cfg: dict, token: str):
        self.http = http
        self.cfg = cfg
        self.token = token

    def fetch(self) -> list[Offer]:
        t = self.cfg["trip"]
        months_out = sorted({d.strftime("%Y-%m") for d in date_window(self.cfg, "outbound")})
        months_ret = sorted({d.strftime("%Y-%m") for d in date_window(self.cfg, "return")})
        markets = self.cfg["sources"]["travelpayouts"].get("markets") or ["us"]
        best: dict[str, Offer] = {}
        errors, calls = [], 0
        for market in markets:
            for mo in months_out:
                for mr in months_ret:
                    calls += 1
                    params = {
                        "origin": t["origin"], "destination": t["destination"],
                        "departure_at": mo, "return_at": mr,
                        "one_way": "false", "direct": "false", "sorting": "price",
                        "currency": t.get("currency", "USD").lower(), "market": market,
                        "limit": 1000, "page": 1,
                    }
                    try:
                        data = self.http.get_json(URL, params=params, headers={"X-Access-Token": self.token},
                                                  timeout=30)
                    except HttpError as e:
                        errors.append(f"{market} {mo}/{mr}: {e}")
                        continue
                    if not data.get("success", False):
                        errors.append(f"{market} {mo}/{mr}: {str(data.get('error'))[:200]}")
                        continue
                    cur = str(data.get("currency", t.get("currency", "USD"))).upper()
                    if cur != t.get("currency", "USD").upper():
                        errors.append(f"unexpected currency {cur}")
                        continue
                    for row in data.get("data") or []:
                        o = self._parse(row)
                        if o and (o.pair_key not in best or o.price < best[o.pair_key].price):
                            best[o.pair_key] = o
        if errors and len(errors) == calls:
            raise SourceError("All Travelpayouts requests failed: " + "; ".join(errors[:3]))
        for e in errors:
            log.warning("Travelpayouts partial failure: %s", e)
        log.info("Aviasales cache: %d date pairs with prices", len(best))
        return list(best.values())

    def _parse(self, row: dict) -> Offer | None:
        t = self.cfg["trip"]
        try:
            out_d = date.fromisoformat(str(row["departure_at"])[:10])
            ret_d = date.fromisoformat(str(row["return_at"])[:10])
            price = float(row["price"])
        except (KeyError, ValueError, TypeError):
            return None
        if not in_window(self.cfg, out_d, ret_d):
            return None
        max_stops = int(t["max_stops_per_direction"])
        max_min = float(t["max_duration_hours_per_direction"]) * 60
        st_out, st_ret = row.get("transfers"), row.get("return_transfers")
        d_out, d_ret = row.get("duration_to"), row.get("duration_back")
        if (st_out is not None and st_out > max_stops) or (st_ret is not None and st_ret > max_stops):
            return None
        if (d_out and d_out > max_min) or (d_ret and d_ret > max_min):
            return None
        code = str(row.get("airline") or "?")
        link = str(row.get("link") or "")
        return Offer(
            source=self.name, out_date=out_d, ret_date=ret_d, price=price,
            airline=AIRLINE_NAMES.get(code, code),
            stops_out=st_out, stops_ret=st_ret, duration_out_min=d_out, duration_ret_min=d_ret,
            link=("https://www.aviasales.com" + (link if link.startswith("/") else "/" + link)) if link else
                 "https://www.aviasales.com/",
            baggage=Baggage(), cached=True,
        )
