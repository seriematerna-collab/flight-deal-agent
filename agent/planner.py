"""Scheduling decisions: is a scan due, how many Google Flights searches can we spend,
and which date pairs to search.

Quota strategy (SerpApi free plan = 250 searches/month):
* A "token bucket" earns monthly_budget / (hours in month) searches per hour, so the
  monthly budget is spread evenly no matter how often the workflow runs.
* Each date pair has a weight: 1 at the preferred dates, falling off with distance.
  Priority = weight x hours since that pair was last searched. On the free plan that is
  about 40 live searches/month for the exact preferred dates, 10-15 for dates 1 day off,
  and ~45 different date pairs per month in total.
* Travelpayouts (free, unlimited for us) covers all 165 pairs on every scan; any cheap
  cached price jumps to the front of the queue for a live Google check.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import date_pairs, date_window
from .storage import iso, parse_iso


def scan_interval_minutes(cfg: dict, today: date) -> int:
    first_out = min(date_window(cfg, "outbound"))
    days_left = (first_out - today).days
    for rule in cfg["schedule"]["closer_intervals"]:   # sorted by days_before ascending
        if days_left <= rule["days_before"]:
            return int(rule["interval_minutes"])
    return int(cfg["schedule"]["base_interval_minutes"])


def scan_due(cfg: dict, state: dict, now: datetime) -> bool:
    last = parse_iso(state.get("last_scan"))
    if last is None:
        return True
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    interval = scan_interval_minutes(cfg, now.astimezone(tz).date())
    # 5 min slack: GitHub cron start times jitter
    return now - last >= timedelta(minutes=interval - 5)


def summary_due(cfg: dict, state: dict, now: datetime) -> bool:
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    local = now.astimezone(tz)
    hh, mm = cfg["schedule"]["_summary_hm"]
    if (local.hour, local.minute) < (hh, mm):
        return False
    return state.get("last_summary_date") != local.date().isoformat()


def accrue_credits(cfg: dict, state: dict, now: datetime) -> float:
    s = state["serpapi"]
    sc = cfg["sources"]["serpapi"]
    hours_in_month = calendar.monthrange(now.year, now.month)[1] * 24
    rate = float(sc["monthly_budget"]) / hours_in_month
    last = parse_iso(s.get("last_accrual"))
    if last is not None:
        hours = max(0.0, (now - last).total_seconds() / 3600)
        s["credits"] = float(s.get("credits", 0)) + hours * rate
    s["credits"] = min(float(s.get("credits", 0)), float(sc["max_bucket"]))
    left = s.get("searches_left")
    if isinstance(left, (int, float)):
        s["credits"] = min(s["credits"], max(0.0, float(left) - 2))   # keep 2 spare on the account
    s["last_accrual"] = iso(now)
    return s["credits"]


def pair_weight(cfg: dict, out_d: date, ret_d: date) -> float:
    t = cfg["trip"]
    do = abs((out_d - t["outbound"]["preferred"]).days) / max(1, t["outbound"]["flex_days"])
    dr = abs((ret_d - t["return"]["preferred"]).days) / max(1, t["return"]["flex_days"])
    k = float(cfg["sources"]["serpapi"].get("focus_strength", 5.0))
    return 1.0 / (1.0 + k * (do + dr)) ** 2


def pick_pairs(cfg: dict, state: dict, now: datetime, n: int,
               exclude: set[str] | None = None) -> list[tuple[date, date]]:
    """The n date pairs with the highest weight x staleness."""
    if n <= 0:
        return []
    exclude = exclude or set()
    last_scan = state["serpapi"]["pair_last_scan"]
    never = 24 * 14.0   # a never-searched pair counts as 14 days stale
    scored = []
    for o, r in date_pairs(cfg, now.date()):
        key = f"{o.isoformat()}|{r.isoformat()}"
        if key in exclude:
            continue
        last = parse_iso(last_scan.get(key))
        hours = never if last is None else max(0.0, (now - last).total_seconds() / 3600)
        w = pair_weight(cfg, o, r)
        scored.append((w * hours, w, o, r))
    scored.sort(reverse=True)
    return [(o, r) for _s, _d, o, r in scored[:n]]
