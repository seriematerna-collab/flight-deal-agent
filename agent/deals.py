"""Deal detection: caps, median, error fares, sudden drops, no-repeat logic, ranking."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .baggage import tier_prices
from .config import days_off_preferred
from .models import Offer, TierPrice
from .storage import Store, iso


@dataclass
class DealHit:
    offer: Offer
    tiers: dict[str, TierPrice]                 # qualifying tiers only
    reasons: dict[str, list[str]]
    usual: dict[str, tuple[float, str] | None]  # tier -> (usual price, how it was computed)
    primary: str
    rank: float
    error_fare: bool = False
    all_tiers: dict[str, TierPrice] = field(default_factory=dict)


def usual_price(store: Store, cfg: dict, tier: str, now: datetime) -> tuple[float, str] | None:
    d = cfg["deals"]
    med, n = store.tier_median(tier, int(d["median_window_days"]), now)
    if med is not None and n >= int(d["median_min_samples"]):
        return med, f"median of {n} recent prices"
    typ = store.state.get("google_typical")
    if tier == "no_bag" and typ and typ.get("low") and typ.get("high"):
        return (typ["low"] + typ["high"]) / 2, "Google Flights typical price"
    return None


def rank_value(cfg: dict, offer: Offer, total: float, tier: str) -> float:
    do, dr = days_off_preferred(cfg, offer.out_date, offer.ret_date)
    penalty = float(cfg["deals"]["date_penalty_usd_per_day"]) * (do + dr)
    return (total + penalty) / cfg["caps_usd"][tier]


def evaluate(offer: Offer, store: Store, cfg: dict, now: datetime) -> DealHit | None:
    """Call BEFORE recording this offer's prices (sudden-drop compares with the previous point)."""
    d = cfg["deals"]
    caps = cfg["caps_usd"]
    tps = tier_prices(offer, cfg)
    if offer.excluded or offer.price < float(d["sanity_min_price_usd"]):
        return None
    qualifying, reasons, usual_map = {}, {}, {}
    error = False
    for tier, tp in tps.items():
        r: list[str] = []
        cap = caps[tier]
        usual = usual_price(store, cfg, tier, now)
        usual_map[tier] = usual
        if tp.total <= cap:
            r.append(f"under your ${cap:,.0f} cap")
        if usual:
            pct = (usual[0] - tp.total) / usual[0] * 100
            if pct >= float(d["below_median_pct"]):
                r.append(f"{pct:.0f}% below usual")
        is_err = (tier == "no_bag" and tp.total < float(d["error_fare_floor_usd"])) or \
                 (usual is not None and tp.total < float(d["error_fare_ratio"]) * usual[0])
        if is_err:
            r.append("possible ERROR FARE")
            error = True
        prev = store.last_observation(f"{offer.pair_key}|{tier}", offer.source)
        if prev:
            drop = prev[1] - tp.total
            if (drop >= float(d["sudden_drop_min_usd"])
                    and drop / prev[1] * 100 >= float(d["sudden_drop_pct"])
                    and tp.total <= cap * float(d["sudden_drop_max_vs_cap"])):
                r.append(f"sudden drop from ${prev[1]:,.0f}")
        if r:
            qualifying[tier] = tp
            reasons[tier] = r
    if not qualifying:
        return None
    primary = min(qualifying, key=lambda t: rank_value(cfg, offer, qualifying[t].total, t))
    return DealHit(offer, qualifying, reasons, usual_map, primary,
                   rank_value(cfg, offer, qualifying[primary].total, primary), error, tps)


def new_tiers(hit: DealHit, store: Store, cfg: dict) -> list[str]:
    """Tiers of this hit that were never alerted, or whose price dropped further."""
    d = cfg["deals"]
    out = []
    for tier, tp in hit.tiers.items():
        prev = store.state["alerts_sent"].get(f"{hit.offer.pair_key}|{tier}")
        if prev is None:
            out.append(tier)
            continue
        need = max(float(d["realert_min_drop_usd"]), prev["price"] * float(d["realert_min_drop_pct"]) / 100)
        if tp.total <= prev["price"] - need:
            out.append(tier)
    return out


def mark_alerted(hit: DealHit, store: Store, now: datetime) -> None:
    for tier, tp in hit.tiers.items():
        key = f"{hit.offer.pair_key}|{tier}"
        prev = store.state["alerts_sent"].get(key)
        if prev is None or tp.total < prev["price"]:
            store.state["alerts_sent"][key] = {"price": round(tp.total, 2), "ts": iso(now)}


def record_pair(offers: list[Offer], store: Store, cfg: dict, now: datetime) -> None:
    """Save price history and the latest price for one date pair: cheapest offer per tier."""
    best: dict[str, tuple[TierPrice, Offer]] = {}
    for o in offers:
        if o.excluded:
            continue
        for tier, tp in tier_prices(o, cfg).items():
            if tier not in best or tp.total < best[tier][0].total:
                best[tier] = (tp, o)
    for tier, (tp, offer) in best.items():
        key = f"{offer.pair_key}|{tier}"
        store.add_observation(key, tp.total, offer.source, now)
        latest = store.state["latest"].get(key)
        stale = latest is None or (now - datetime.fromisoformat(latest["ts"])).total_seconds() > 24 * 3600
        # a live price always replaces a cached one; a cached one never replaces a fresh live one
        if stale or not offer.cached or latest.get("cached", True):
            store.state["latest"][key] = {
                "price": round(tp.total, 2), "ts": iso(now), "source": offer.source, "cached": offer.cached,
                "note": tp.note, "verified": tp.verified, "airline": offer.airline,
                "stops_out": offer.stops_out, "stops_ret": offer.stops_ret, "link": offer.link,
            }


def consolidate(hits: list[DealHit], cfg: dict) -> list[DealHit]:
    """For each date pair and baggage tier keep only the cheapest hit (live beats cached on ties),
    then group the winning tiers by offer: one alert per distinct fare, never one per tier."""
    best: dict[tuple[str, str], DealHit] = {}
    for h in hits:
        for tier, tp in h.tiers.items():
            k = (h.offer.pair_key, tier)
            cur = best.get(k)
            if cur is None:
                best[k] = h
                continue
            cur_tp = cur.tiers[tier]
            if tp.total < cur_tp.total - 0.5 or (abs(tp.total - cur_tp.total) <= 0.5 and cur.offer.cached
                                                  and not h.offer.cached):
                best[k] = h
    grouped: dict[int, tuple[DealHit, list[str]]] = {}
    for (_pair, tier), h in best.items():
        grouped.setdefault(id(h), (h, []))[1].append(tier)
    out = []
    for h, tiers in grouped.values():
        sub = {t: h.tiers[t] for t in tiers}
        primary = min(sub, key=lambda t: rank_value(cfg, h.offer, sub[t].total, t))
        out.append(DealHit(h.offer, sub, {t: h.reasons[t] for t in tiers}, h.usual, primary,
                           rank_value(cfg, h.offer, sub[primary].total, primary),
                           any("ERROR FARE" in r for t in tiers for r in h.reasons[t]), h.all_tiers))
    out.sort(key=lambda h: h.rank)
    return out
