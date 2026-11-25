"""Load and validate config.yaml, plus small date helpers."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

TIERS = ("no_bag", "carry_on", "checked")
TIER_LABELS = {"no_bag": "No bag (personal item)", "carry_on": "Carry-on bag", "checked": "Checked bag"}


class ConfigError(Exception):
    pass


@dataclass
class Secrets:
    email_address: str | None
    email_app_password: str | None
    email_to: str | None
    serpapi_key: str | None
    travelpayouts_token: str | None

    @classmethod
    def from_env(cls) -> "Secrets":
        def get(name: str) -> str | None:
            v = os.environ.get(name, "").strip()
            return v or None

        return cls(
            email_address=get("EMAIL_ADDRESS"),
            email_app_password=get("EMAIL_APP_PASSWORD"),
            email_to=get("EMAIL_TO"),
            serpapi_key=get("SERPAPI_KEY"),
            travelpayouts_token=get("TRAVELPAYOUTS_TOKEN"),
        )

    def values(self) -> list[str]:
        return [v for v in (self.email_app_password, self.serpapi_key, self.travelpayouts_token) if v]


def _to_date(v: Any, field: str) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError as e:
        raise ConfigError(f"{field}: expected YYYY-MM-DD, got {v!r}") from e


def load_config(path: str | Path = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    with p.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    try:
        trip = cfg["trip"]
        for leg in ("outbound", "return"):
            trip[leg]["preferred"] = _to_date(trip[leg]["preferred"], f"trip.{leg}.preferred")
            trip[leg]["flex_days"] = int(trip[leg]["flex_days"])
            if trip[leg]["flex_days"] < 0:
                raise ConfigError(f"trip.{leg}.flex_days must be >= 0")
        alts = trip.get("alternative_destinations") or []
        if isinstance(alts, str):
            alts = [a.strip() for a in alts.split(",") if a.strip()]
        trip["destinations"] = [trip["destination"]] + [a for a in alts if a != trip["destination"]]
        if trip["return"]["preferred"] <= trip["outbound"]["preferred"]:
            raise ConfigError("trip.return.preferred must be after trip.outbound.preferred")
        for t in TIERS:
            cfg["caps_usd"][t] = float(cfg["caps_usd"][t])
        cfg.setdefault("bag_fees", {})
        cfg["bag_fees"] = cfg["bag_fees"] or {}
        cfg["schedule"]["closer_intervals"] = sorted(
            cfg["schedule"].get("closer_intervals") or [], key=lambda r: r["days_before"]
        )
        hh, mm = str(cfg["schedule"]["daily_summary_time"]).split(":")
        cfg["schedule"]["_summary_hm"] = (int(hh), int(mm))
    except KeyError as e:
        raise ConfigError(f"Missing config key: {e}") from e
    return cfg


def google_flights_link(cfg: dict, out_d, ret_d, dest: str | None = None) -> str:
    """Plain Google Flights search link for one route + dates (used when no booking link is known)."""
    from urllib.parse import quote
    t = cfg["trip"]
    q = f"Flights from {t['origin']} to {dest or t['destination']} on {out_d.isoformat()} through {ret_d.isoformat()}"
    return f"https://www.google.com/travel/flights?q={quote(q)}&curr={t.get('currency', 'USD')}"


def date_window(cfg: dict, leg: str) -> list[date]:
    leg_cfg = cfg["trip"][leg]
    p, flex = leg_cfg["preferred"], leg_cfg["flex_days"]
    return [p + timedelta(days=d) for d in range(-flex, flex + 1)]


def date_pairs(cfg: dict, today: date | None = None) -> list[tuple[date, date]]:
    """All (outbound, return) combinations still in the future."""
    today = today or date.today()
    pairs = []
    for o in date_window(cfg, "outbound"):
        if o <= today:
            continue
        for r in date_window(cfg, "return"):
            if r > o:
                pairs.append((o, r))
    return pairs


def days_off_preferred(cfg: dict, out_d: date, ret_d: date) -> tuple[int, int]:
    return (
        abs((out_d - cfg["trip"]["outbound"]["preferred"]).days),
        abs((ret_d - cfg["trip"]["return"]["preferred"]).days),
    )


def in_window(cfg: dict, out_d: date, ret_d: date) -> bool:
    o, r = days_off_preferred(cfg, out_d, ret_d)
    return o <= cfg["trip"]["outbound"]["flex_days"] and r <= cfg["trip"]["return"]["flex_days"]
