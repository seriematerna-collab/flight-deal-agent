"""Text of every message (simple HTML: <b>, <a>, <code>, newlines)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from .config import TIER_LABELS, TIERS, in_window
from .deals import DealHit, rank_value
from .models import FeedDeal, Offer
from .planner import scan_interval_minutes
from .storage import Store, parse_iso

SOURCE_LABELS = {"google_flights": "Google Flights (live)", "aviasales_cache": "Aviasales (cached price)"}


def money(x: float | None) -> str:
    return "?" if x is None else f"${x:,.0f}"


def dur(m: int | None) -> str:
    if not m:
        return "?"
    return f"{m // 60}h {m % 60:02d}m"


def stops(n: int | None) -> str:
    if n is None:
        return "stops ?"
    return "nonstop" if n == 0 else f"{n} stop" + ("s" if n > 1 else "")


def day(d: date) -> str:
    return d.strftime("%a %d %b %Y")


def ago(ts: str | None, now: datetime) -> str:
    t = parse_iso(ts)
    if not t:
        return "never"
    mins = int((now - t).total_seconds() // 60)
    if mins < 60:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} days ago"


def booking_buttons(link: str) -> list[list[dict]]:
    rows = []
    if link:
        rows.append([{"text": "Open booking page", "url": link}])
    rows.append([{"text": "Status", "callback_data": "status"},
                 {"text": "Pause alerts", "callback_data": "pause"}])
    return rows


def format_alert(hit: DealHit, cfg: dict) -> str:
    o: Offer = hit.offer
    t = cfg["trip"]
    p = hit.tiers[hit.primary]
    head = "🚨 POSSIBLE ERROR FARE" if hit.error_fare else "✈️ Flight deal"
    lines = [f"<b>{head}: {escape(t['origin'])} ⇄ {escape(t['destination'])} {money(p.total)}</b>",
             f"Baggage tier: <b>{TIER_LABELS[hit.primary]}</b>"]
    for tier in TIERS:
        if tier not in hit.tiers:
            continue
        tp = hit.tiers[tier]
        usual = hit.usual.get(tier)
        below = ""
        if usual:
            pct = (usual[0] - tp.total) / usual[0] * 100
            below = (f" · {pct:.0f}% below usual {money(usual[0])}" if pct > 0
                     else f" · {-pct:.0f}% above usual {money(usual[0])}")
        lines.append(f"• {TIER_LABELS[tier]}: <b>{money(tp.total)}</b> ({escape(tp.note)}){below}")
        lines.append(f"   why: {escape(', '.join(hit.reasons[tier]))}")
    if not o.baggage.known():
        lines.append("⚠️ <b>baggage unverified</b>: check what the fare includes before booking")
    lines += [
        f"Airline: {escape(o.airline)}",
        f"Outbound: {day(o.out_date)} · {stops(o.stops_out)} · {dur(o.duration_out_min)}",
    ]
    if o.stops_ret is not None or o.duration_ret_min:
        lines.append(f"Return: {day(o.ret_date)} · {stops(o.stops_ret)} · {dur(o.duration_ret_min)}")
        if o.duration_out_min and o.duration_ret_min:
            lines.append(f"Total travel time: {dur(o.duration_out_min + o.duration_ret_min)}")
    else:
        lines.append(f"Return: {day(o.ret_date)} · details not fetched (open the link)")
    lines.append(f"Source: {SOURCE_LABELS.get(o.source, o.source)}")
    if o.cached:
        lines.append("ℹ️ Cached price: it may have changed. Not re-checked live yet.")
    return "\n".join(lines)


def format_bundle(hits: list[DealHit], cfg: dict) -> str:
    lines = [f"<b>➕ {len(hits)} more deals this run</b>"]
    for h in hits:
        tp = h.tiers[h.primary]
        lines.append(f"• {money(tp.total)} {TIER_LABELS[h.primary]} · {h.offer.out_date:%d %b} → "
                     f"{h.offer.ret_date:%d %b} · {escape(h.offer.airline)} · "
                     f"<a href=\"{escape(h.offer.link, quote=True)}\">link</a>")
    return "\n".join(lines)


def format_feed(d: FeedDeal) -> str:
    head = "🚨 Possible error fare (deal blog)" if d.error_fare else "📰 Deal blog post"
    lines = [f"<b>{head}</b>", escape(d.title)]
    if d.price_usd:
        lines.append(f"Price mentioned: {money(d.price_usd)} (check dates and baggage; may not match your trip)")
    return "\n".join(lines)


def _fresh_latest(store: Store, cfg: dict, tier: str, now: datetime) -> list[tuple[float, str, dict]]:
    max_age = timedelta(days=int(cfg["schedule"]["summary_max_age_days"]))
    rows = []
    for key, v in store.state["latest"].items():
        pair, t = key.rsplit("|", 1)
        if t != tier:
            continue
        ts = parse_iso(v.get("ts"))
        if not ts or now - ts > max_age:
            continue
        o_s, r_s = pair.split("|")
        o_d, r_d = date.fromisoformat(o_s), date.fromisoformat(r_s)
        if o_d <= now.date() or not in_window(cfg, o_d, r_d):
            continue
        fake = Offer(source=v["source"], out_date=o_d, ret_date=r_d, price=v["price"])
        rows.append((rank_value(cfg, fake, v["price"], tier), pair, v))
    rows.sort(key=lambda r: r[0])
    return rows


def _trend(store: Store, tier: str, now: datetime) -> str:
    dm = store.state["daily_min"].get(tier) or {}
    if not dm:
        return "no history yet"
    days = sorted(dm)
    last = days[-1]
    target = (now.date() - timedelta(days=7)).isoformat()
    older = [d for d in days if d <= target]
    if not older:
        return f"cheapest seen {money(dm[last])} (less than a week of history)"
    then = dm[older[-1]]
    change = (dm[last] - then) / then * 100
    arrow = "↓" if change < -1 else "↑" if change > 1 else "→"
    return f"cheapest seen {money(dm[last])} vs {money(then)} a week ago ({arrow} {abs(change):.0f}%)"


def format_summary(store: Store, cfg: dict, now: datetime) -> str:
    n = int(cfg["schedule"]["summary_top_n"])
    t = cfg["trip"]
    lines = [f"<b>☀️ Daily flight summary {escape(t['origin'])} ⇄ {escape(t['destination'])}</b>"]
    for tier in TIERS:
        cap = cfg["caps_usd"][tier]
        lines.append(f"\n<b>{TIER_LABELS[tier]}</b> (cap {money(cap)})")
        rows = _fresh_latest(store, cfg, tier, now)[:n]
        if not rows:
            lines.append("  no prices yet for this tier" + (
                " (needs bag info: see README, 'bag_fees')" if tier != "no_bag" else ""))
        for i, (_r, pair, v) in enumerate(rows, 1):
            o_s, r_s = pair.split("|")
            flag = "✓" if v.get("verified") else "⚠️ unverified"
            under = " ✅" if v["price"] <= cap else ""
            lines.append(f"  {i}. <b>{money(v['price'])}</b>{under} {date.fromisoformat(o_s):%d %b} → "
                         f"{date.fromisoformat(r_s):%d %b} · {escape(str(v.get('airline', '?')))} · "
                         f"{'cached' if v.get('cached') else 'live'} {ago(v['ts'], now)} · {flag}")
        lines.append(f"  Trend: {_trend(store, tier, now)}")
    left = store.state["serpapi"].get("searches_left")
    if left is not None:
        lines.append(f"\nGoogle Flights searches left this month: {left}")
    if store.state.get("paused"):
        lines.append("⏸ Alerts are paused. Email yourself \"flights resume\" to restart.")
    return "\n".join(lines)


def format_status(store: Store, cfg: dict, now: datetime) -> str:
    s = store.state
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    interval = scan_interval_minutes(cfg, now.astimezone(tz).date())
    lines = ["<b>📊 Flight agent status</b>",
             f"State: {'⏸ PAUSED' if s.get('paused') else '▶️ running'}",
             f"Last run: {ago(s.get('last_run'), now)} · last full scan: {ago(s.get('last_scan'), now)}",
             f"Scan interval now: every {interval} min",
             "", "<b>Cheapest now</b>"]
    for tier in TIERS:
        rows = _fresh_latest(store, cfg, tier, now)
        if rows:
            _r, pair, v = rows[0]
            o_s, r_s = pair.split("|")
            lines.append(f"• {TIER_LABELS[tier]}: <b>{money(v['price'])}</b> {date.fromisoformat(o_s):%d %b} → "
                         f"{date.fromisoformat(r_s):%d %b} ({escape(str(v.get('airline', '?')))}, "
                         f"{ago(v['ts'], now)})")
        else:
            lines.append(f"• {TIER_LABELS[tier]}: no data yet")
    lines.append("")
    lines.append("<b>Sources</b>")
    for name, h in sorted(s.get("sources", {}).items()):
        ok = h.get("fail_streak", 0) == 0
        lines.append(f"• {name}: {'OK' if ok else '❌ failing x' + str(h.get('fail_streak'))} "
                     f"(last OK {ago(h.get('last_ok'), now)})")
    sp = s["serpapi"]
    lines.append(f"Google searches: {sp.get('credits', 0):.1f} ready now, "
                 f"{sp.get('searches_left', '?')} left on account")
    return "\n".join(lines)


HELP = ("<b>How to control the agent by email</b>\n"
        "Send an email to yourself with one of these subjects (the body doesn't matter):\n"
        "• <b>flights status</b>: last run and cheapest prices now\n"
        "• <b>flights pause</b>: stop scans and alerts\n"
        "• <b>flights resume</b>: start again\n"
        "• <b>flights help</b>: this message\n"
        "Spanish also works: vuelos estado / vuelos pausa / vuelos reanudar.\n\n"
        "The agent checks your inbox every ~30 minutes, so replies can take a while.")


def source_warning(name: str, streak: int, err: str) -> str:
    return (f"⚠️ <b>Source '{escape(name)}' has failed {streak} runs in a row.</b>\n"
            f"Last error: <code>{escape(err[:300])}</code>\nOther sources keep working. See README → "
            f"'What can break'.")
