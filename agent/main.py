"""Entry point.  python -m agent.main [--dry-run] [--test-alert] [--force-scan] [--summary-now] [--demo]
                                     [--status] [--pause] [--resume]

One run = read command emails -> (if due) scan sources, detect deals, alert -> (if due)
daily summary -> save state. The GitHub workflow commits data/state.json afterwards.
Exit code 1 when an email could not be sent (so the Actions run shows as failed).
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from . import messages as M
from .config import ConfigError, Secrets, date_pairs, google_flights_link, in_window, load_config
from .deals import DealHit, consolidate, evaluate, mark_alerted, new_tiers, record_pair
from .http import Http
from .models import Baggage, Offer
from .planner import accrue_credits, pick_pairs, scan_due, summary_due
from .sources.feeds import DealFeeds
from .sources.serpapi_google import GoogleFlights
from .sources.travelpayouts import AviasalesCache
from .storage import Store, iso, parse_iso, utcnow
from .notify_email import EmailNotifier

log = logging.getLogger("agent")


# --------------------------------------------------------------------------- commands
def process_commands(notifier: EmailNotifier, store: Store, extra: list[str] | None = None) -> bool:
    """Handle pause/resume/help now; return True if status was requested (answered at end of run)."""
    want_status = False
    cmds = notifier.read_commands(store.state) + list(extra or [])
    for cmd in cmds:
        log.info("Command: %s", cmd)
        if cmd == "pause":
            store.state["paused"] = True
            notifier.send("⏸ <b>Paused.</b> No scans or alerts until you resume.",
                          [[{"text": "Resume", "callback_data": "resume"}]], subject="⏸ Flight alerts paused")
        elif cmd == "resume":
            store.state["paused"] = False
            store.state["last_scan"] = None     # scan right away
            notifier.send("▶️ <b>Resumed.</b> Scanning now.", subject="▶️ Flight alerts resumed")
        elif cmd == "status":
            want_status = True
        elif cmd == "help":
            notifier.send(M.HELP, subject="How to control the flight agent")
    return want_status


# --------------------------------------------------------------------------- scan
class ScanContext:
    def __init__(self, cfg, secrets, http, tg, store, now, bonus_searches: int = 0):
        self.cfg, self.secrets, self.http, self.tg, self.store, self.now = cfg, secrets, http, tg, store, now
        self.bonus_searches = bonus_searches
        self.hits: list[DealHit] = []

    def ok(self, name: str) -> None:
        self.store.source_ok(name, self.now)

    def fail(self, name: str, err: Exception) -> None:
        msg = self.http.redact(f"{type(err).__name__}: {err}")
        log.error("Source %s failed: %s", name, msg)
        streak = self.store.source_failed(name, msg, self.now)
        h = self.store.state["sources"][name]
        if streak >= int(self.cfg["sources"].get("failure_warning_after", 6)) and not h.get("warned"):
            if self.tg.send(M.source_warning(name, streak, msg)):
                h["warned"] = True


def pair_hits(offers: list[Offer], ctx: ScanContext) -> list[DealHit]:
    """Deals among one date pair's offers: best offer per baggage tier."""
    return consolidate([h for h in (evaluate(o, ctx.store, ctx.cfg, ctx.now) for o in offers) if h], ctx.cfg)


def scan_feeds(ctx: ScanContext) -> None:
    fc = ctx.cfg["sources"]["feeds"]
    if not fc.get("enabled"):
        return
    src = DealFeeds(ctx.http, ctx.cfg)
    try:
        deals = src.fetch(ctx.now)
    except Exception as e:  # noqa: BLE001 - a broken source must never stop the others
        ctx.fail(src.name, e)
        return
    ctx.ok(src.name)
    seen = ctx.store.state["feed_seen"]
    for d in deals:
        if d.link in seen:
            continue
        if ctx.tg.send(M.format_feed(d), M.booking_buttons(d.link), subject=f"📰 {d.title[:120]}"):
            seen[d.link] = iso(ctx.now)


def scan_aviasales(ctx: ScanContext, serpapi_ready: bool) -> None:
    tc = ctx.cfg["sources"]["travelpayouts"]
    if not tc.get("enabled"):
        return
    if not ctx.secrets.travelpayouts_token:
        log.warning("TRAVELPAYOUTS_TOKEN not set: skipping Aviasales cache")
        return
    src = AviasalesCache(ctx.http, ctx.cfg, ctx.secrets.travelpayouts_token)
    try:
        offers = src.fetch()
    except Exception as e:  # noqa: BLE001
        ctx.fail(src.name, e)
        return
    ctx.ok(src.name)
    queue = ctx.store.state["verify_queue"]
    verify = tc.get("verify_with_serpapi", True) and serpapi_ready
    wait = timedelta(hours=float(tc.get("cached_alert_after_hours", 12)))
    for o in offers:
        hit = evaluate(o, ctx.store, ctx.cfg, ctx.now)
        record_pair([o], ctx.store, ctx.cfg, ctx.now)
        if not hit or not new_tiers(hit, ctx.store, ctx.cfg):
            continue
        if not verify:
            ctx.hits.append(hit)
            continue
        q = queue.setdefault(o.pair_key, {"since": iso(ctx.now), "price": o.price})
        if q.get("checked_price") is not None and o.price >= q["checked_price"]:
            continue    # this cached price was already checked live and didn't hold up
        if o.price != q.get("price"):
            q.update({"since": iso(ctx.now), "price": o.price})
            q.pop("checked_price", None)
        if ctx.now - (parse_iso(q["since"]) or ctx.now) >= wait:
            log.info("Cached deal %s not verified within %s: alerting as cached", o.pair_key, wait)
            ctx.hits.append(hit)
            queue.pop(o.pair_key, None)


def scan_google(ctx: ScanContext) -> None:
    sc = ctx.cfg["sources"]["serpapi"]
    if not sc.get("enabled"):
        return
    if not ctx.secrets.serpapi_key:
        log.warning("SERPAPI_KEY not set: skipping Google Flights")
        return
    gf = GoogleFlights(ctx.http, ctx.cfg, ctx.secrets.serpapi_key)
    state = ctx.store.state["serpapi"]
    left = gf.searches_left()
    if left is not None:
        state["searches_left"] = left
    credits = accrue_credits(ctx.cfg, ctx.store.state, ctx.now)
    if ctx.bonus_searches:
        # manual "search now": borrow from future days (the balance goes negative and refills over time)
        cap = credits + ctx.bonus_searches
        left_now = state.get("searches_left")
        if isinstance(left_now, int):
            cap = min(cap, max(0, left_now - 2))
        credits = max(credits, cap)
    budget = int(credits)
    enrich_on = bool(sc.get("enrich_alerts", True))
    scan_n = budget - 2 if (enrich_on and budget >= 3) else budget
    log.info("Google Flights: %.2f searches available, scanning %d date pairs", credits, scan_n)

    # verification queue first (cached deals to confirm), then weighted rotation
    queue = ctx.store.state["verify_queue"]
    valid = {f"{o.isoformat()}|{r.isoformat()}" for o, r in date_pairs(ctx.cfg, ctx.now.date())}
    week_ago = ctx.now - timedelta(days=8)
    for k in [k for k, q in queue.items() if k not in valid or (parse_iso(q.get("since")) or ctx.now) < week_ago]:
        del queue[k]
    pending = [k for k, q in queue.items() if q.get("checked_price") is None]
    first = sorted(pending, key=lambda k: queue[k].get("price", 1e9))[:scan_n]
    pairs = [tuple(date.fromisoformat(x) for x in k.split("|")) for k in first]
    pairs += pick_pairs(ctx.cfg, ctx.store.state, ctx.now, scan_n - len(pairs), exclude=set(first))

    enriched = 0
    failed = None
    for o_d, r_d in pairs:
        if credits - gf.searches_used < 1:
            break    # enrichments used up this run's share
        key = f"{o_d.isoformat()}|{r_d.isoformat()}"
        try:
            offers, meta = gf.search_pair(o_d, r_d)
        except Exception as e:  # noqa: BLE001
            failed = e
            break
        state["pair_last_scan"][key] = iso(ctx.now)
        if key in queue:
            queue[key]["checked_price"] = queue[key].get("price")
        rng = (meta.get("price_insights") or {}).get("typical_price_range")
        if isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, (int, float)) for x in rng):
            ctx.store.state["google_typical"] = {"low": rng[0], "high": rng[1], "ts": iso(ctx.now)}
        if not offers:
            continue
        hits = pair_hits(offers, ctx)
        # Spend 2 searches (return leg + bag prices) on promising fares. Fares with unknown baggage
        # go first: a cheap fare might include a bag and beat the carry-on/checked caps.
        max_cap = max(ctx.cfg["caps_usd"].values())
        cands: list[Offer] = []
        for o in [h.offer for h in hits] + [o for o in offers if o.price <= max_cap and not o.baggage.known()][:1]:
            if all(o is not c for c in cands):
                cands.append(o)
        cands.sort(key=lambda o: (o.baggage.known(), o.price))
        changed = False
        for cand in cands:
            ek = f"{key}|{cand.airline}|{round(cand.price, -1):.0f}"
            if (not enrich_on or enriched >= int(sc.get("max_enrichments_per_run", 2))
                    or credits - gf.searches_used < 2 or ek in ctx.store.state["enrich_cache"]):
                continue
            try:
                gf.enrich(cand)
                changed = True
            except Exception as e:  # noqa: BLE001 - enrichment is optional
                log.warning("Enrichment failed for %s: %s", key, ctx.http.redact(str(e)))
            enriched += 1
            ctx.store.state["enrich_cache"][ek] = iso(ctx.now)
        if changed:
            hits = pair_hits(offers, ctx)
        record_pair(offers, ctx.store, ctx.cfg, ctx.now)
        ctx.hits.extend(hits)

    state["credits"] = max(-30.0, float(state.get("credits", 0)) - gf.searches_used) if ctx.bonus_searches \
        else max(0.0, credits - gf.searches_used)
    if isinstance(state.get("searches_left"), int):
        state["searches_left"] = max(0, state["searches_left"] - gf.searches_used)
    log.info("Google Flights: used %d searches (%d enrichments)", gf.searches_used, enriched)
    if failed:
        ctx.fail(gf.name, failed)
    elif pairs:
        ctx.ok(gf.name)


def send_alerts(ctx: ScanContext) -> None:
    # best fare per date pair + tier, one message per fare, only if new or cheaper than last alert
    hits = consolidate([h for h in ctx.hits if in_window(ctx.cfg, h.offer.out_date, h.offer.ret_date)], ctx.cfg)
    fresh = [h for h in hits if new_tiers(h, ctx.store, ctx.cfg)]
    fresh.sort(key=lambda h: (not h.error_fare, h.rank))
    n = int(ctx.cfg["deals"]["max_individual_alerts_per_run"])
    for h in fresh[:n]:
        link = h.offer.link or google_flights_link(ctx.cfg, h.offer.out_date, h.offer.ret_date, h.offer.destination)
        if ctx.tg.send(M.format_alert(h, ctx.cfg), M.booking_buttons(link)):
            mark_alerted(h, ctx.store, ctx.now)
    rest = fresh[n:]
    if rest and ctx.tg.send(M.format_bundle(rest, ctx.cfg)):
        for h in rest:
            mark_alerted(h, ctx.store, ctx.now)
    log.info("Deals found: %d, new or cheaper: %d", len(hits), len(fresh))


def run_scan(ctx: ScanContext) -> None:
    ctx.store.state["last_scan"] = iso(ctx.now)
    serpapi_ready = bool(ctx.cfg["sources"]["serpapi"].get("enabled") and ctx.secrets.serpapi_key)
    scan_feeds(ctx)
    scan_aviasales(ctx, serpapi_ready)
    scan_google(ctx)
    send_alerts(ctx)


# --------------------------------------------------------------------------- test alert
def test_alert(cfg: dict, tg: EmailNotifier) -> int:
    if not tg.dry_run:
        if not tg.configured:
            log.error("Set EMAIL_ADDRESS and EMAIL_APP_PASSWORD first (GitHub Secrets).")
            return 1
        try:
            tg.check_login()
            log.info("Email login OK for %s", tg.address)
        except Exception as e:  # noqa: BLE001
            log.error("Email login failed: %s. Check EMAIL_ADDRESS and EMAIL_APP_PASSWORD "
                      "(must be a Google app password, not your normal password).", e)
            return 1
    t = cfg["trip"]
    offer = Offer(source="google_flights", out_date=t["outbound"]["preferred"], ret_date=t["return"]["preferred"],
                  price=cfg["caps_usd"]["no_bag"] - 51, airline="Sample Airways", stops_out=1, stops_ret=1,
                  duration_out_min=14 * 60 + 20, duration_ret_min=13 * 60 + 5,
                  link="https://www.google.com/travel/flights",
                  baggage=Baggage(carry_on="included", checked="fee", checked_fee=60, source="google-booking"))
    store = Store(Path(tempfile.mkdtemp()) / "test.json")
    hit = evaluate(offer, store, cfg, utcnow())
    assert hit is not None
    text = "🧪 <b>TEST ALERT: not a real fare</b>\n\n" + M.format_alert(hit, cfg) + "\n\n" + M.HELP
    ok = tg.send(text, M.booking_buttons(offer.link), subject="🧪 Test alert: your flight agent works")
    if ok:
        log.info("Test alert %s.", "printed (dry run)" if tg.dry_run else "sent: check your inbox")
    return 0 if ok else 1


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Flight deal agent")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending; don't save state")
    ap.add_argument("--test-alert", action="store_true", help="send one sample alert and exit")
    ap.add_argument("--force-scan", action="store_true", help="scan now even if not due")
    ap.add_argument("--summary-now", action="store_true", help="send the daily summary now")
    ap.add_argument("--demo", action="store_true", help="offline demo with sample data (implies --dry-run)")
    ap.add_argument("--status", action="store_true", help="email the status now")
    ap.add_argument("--pause", action="store_true", help="pause scans and alerts")
    ap.add_argument("--resume", action="store_true", help="resume scans and alerts")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        log.error("Config problem: %s", e)
        return 2

    secrets = Secrets.from_env()
    if args.demo:
        from .demo import FixtureHttp, demo_secrets
        args.dry_run = True
        args.force_scan = True
        args.summary_now = True
        secrets = demo_secrets()
        http: Http = FixtureHttp(cfg)
        state_path = Path(tempfile.mkdtemp()) / "state.json"
    else:
        http = Http(secrets.values())
        state_path = Path(cfg["storage"]["path"])

    tg = EmailNotifier(cfg, secrets.email_address, secrets.email_app_password, secrets.email_to,
                       dry_run=args.dry_run)

    if args.test_alert:
        return test_alert(cfg, tg)

    store = Store(state_path, int(cfg["storage"].get("history_points_per_key", 30)))
    now = utcnow()
    extra = [c for c, on in (("pause", args.pause), ("resume", args.resume), ("status", args.status)) if on]
    want_status = process_commands(tg, store, extra)

    if store.state.get("paused") and not args.force_scan:
        log.info('Paused: skipping scan and summary (email yourself "flights resume")')
    else:
        if args.force_scan or scan_due(cfg, store.state, now):
            bonus = int(cfg["sources"]["serpapi"].get("manual_scan_searches", 3)) if args.force_scan else 0
            run_scan(ScanContext(cfg, secrets, http, tg, store, now, bonus))
            if args.force_scan:
                args.summary_now = True     # a manual search always emails you the results
        else:
            log.info("No scan due yet (last scan %s)", store.state.get("last_scan"))
        if args.summary_now or summary_due(cfg, store.state, now):
            if tg.send(M.format_summary(store, cfg, now)):
                from zoneinfo import ZoneInfo
                store.state["last_summary_date"] = now.astimezone(
                    ZoneInfo(cfg["schedule"]["timezone"])).date().isoformat()
    if want_status:
        tg.send(M.format_status(store, cfg, now),
                [[{"text": "Resume" if store.state.get("paused") else "Pause alerts",
                   "callback_data": "resume" if store.state.get("paused") else "pause"},
                  {"text": "Status again", "callback_data": "status"}]], subject="📊 Flight agent status")

    store.state["last_run"] = iso(now)
    if args.dry_run:
        log.info("Dry run: state not saved")
    else:
        store.save()

    if tg.failures:
        for f in tg.failures:
            log.error("Email delivery failed: %s", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
