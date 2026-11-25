"""Run with:  pip install -r requirements-dev.txt && pytest -q"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent import main as agent_main
from agent.baggage import parse_baggage_strings, tier_prices
from agent.config import Secrets, date_pairs, load_config
from agent.deals import consolidate, evaluate, mark_alerted, new_tiers
from agent.demo import FixtureHttp
from agent.models import Baggage, Offer
from agent.planner import accrue_credits, pair_weight, pick_pairs, scan_interval_minutes, summary_due
from agent.sources.feeds import DealFeeds, parse_feed
from agent.sources.serpapi_google import GoogleFlights
from agent.sources.travelpayouts import AviasalesCache
from agent.storage import Store, iso
from agent.notify_email import EmailNotifier, parse_command

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    c = load_config(ROOT / "config.yaml")
    c["sources"]["travelpayouts"]["enabled"] = True   # tests cover every source, even if disabled in config
    c["sources"]["feeds"]["enabled"] = True
    return c


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state.json")


def offer(price, out=date(2027, 2, 1), ret=date(2027, 4, 1), bag=None, source="google_flights", cached=False):
    return Offer(source=source, out_date=out, ret_date=ret, price=price, airline="LATAM",
                 stops_out=1, duration_out_min=900, link="https://x", baggage=bag or Baggage(), cached=cached)


# ---------------------------------------------------------------- config / dates
def test_window_has_165_pairs(cfg):
    assert len(date_pairs(cfg, date(2026, 11, 24))) == 11 * 15


# ---------------------------------------------------------------- baggage
@pytest.mark.parametrize("strings,carry,checked,fee", [
    (["1 free carry-on", "1st checked bag: 99-187"], "included", "fee", 187),
    (["1st checked bag up to 23 kg free"], "unknown", "included", None),
    (["Checked baggage for a fee", "Bag and fare conditions depend on the return flight"], "unknown", "fee", None),
    (["Carry-on bag not included"], "fee", "unknown", None),
    (["No checked bags"], "unknown", "fee", None),
    (["1st checked bag: $35"], "unknown", "fee", 35),
    ([], "unknown", "unknown", None),
])
def test_parse_baggage(strings, carry, checked, fee):
    b = parse_baggage_strings(strings, "t")
    assert (b.carry_on, b.checked, b.checked_fee) == (carry, checked, fee)


def test_unknown_baggage_is_no_bag_only_and_unverified(cfg):
    tp = tier_prices(offer(700), cfg)
    assert list(tp) == ["no_bag"]
    assert not tp["no_bag"].verified and "unverified" in tp["no_bag"].note


def test_bag_fee_added_round_trip(cfg):
    b = Baggage(carry_on="included", checked="fee", checked_fee=70)
    tp = tier_prices(offer(742, bag=b), cfg)
    assert tp["carry_on"].total == 742
    assert tp["checked"].total == 742 + 140


def test_config_fee_table_is_marked_estimated(cfg):
    cfg["bag_fees"] = {"LATAM": {"carry_on": 0, "checked": 60}}
    tp = tier_prices(offer(900), cfg)
    assert tp["checked"].total == 1020 and not tp["checked"].verified


# ---------------------------------------------------------------- deal rules
def test_cap_rule_and_no_cross_tier_compare(cfg, store):
    assert evaluate(offer(790), store, cfg, NOW) is not None           # under no-bag cap
    assert evaluate(offer(1050), store, cfg, NOW) is None              # unknown bags: NOT vs the 1100 checked cap
    checked = Baggage(checked="included")
    hit = evaluate(offer(1050, bag=checked), store, cfg, NOW)
    assert hit and list(hit.tiers) == ["checked"]


def test_median_rule(cfg, store):
    for i in range(25):
        store.add_observation(f"2027-02-0{1 + i % 5}|2027-04-0{1 + i % 7}|no_bag", 1200 + i, "google_flights",
                              NOW - timedelta(days=i % 20, hours=1))
    hit = evaluate(offer(950), store, cfg, NOW)    # above the $800 cap but ~21% below median
    assert hit and any("below usual" in r for r in hit.reasons["no_bag"])
    assert evaluate(offer(1100), store, cfg, NOW) is None


def test_error_fare(cfg, store):
    hit = evaluate(offer(390), store, cfg, NOW)
    assert hit.error_fare
    assert evaluate(offer(50), store, cfg, NOW) is None    # glitch, ignored


def test_sudden_drop(cfg, store):
    key = "2027-02-01|2027-04-01|no_bag"
    store.add_observation(key, 960, "google_flights", NOW - timedelta(hours=3))
    hit = evaluate(offer(850), store, cfg, NOW)
    assert hit and any("sudden drop" in r for r in hit.reasons["no_bag"])
    store2 = Store(store.path.parent / "s2.json")
    store2.add_observation(key, 2000, "google_flights", NOW - timedelta(hours=3))
    assert evaluate(offer(1700), store2, cfg, NOW) is None   # big drop but still far above cap


def test_no_repeat_unless_cheaper(cfg, store):
    hit = evaluate(offer(780), store, cfg, NOW)
    assert new_tiers(hit, store, cfg) == ["no_bag"]
    mark_alerted(hit, store, NOW)
    assert new_tiers(evaluate(offer(780), store, cfg, NOW), store, cfg) == []
    assert new_tiers(evaluate(offer(770), store, cfg, NOW), store, cfg) == []    # only $10 cheaper
    assert new_tiers(evaluate(offer(755), store, cfg, NOW), store, cfg) == ["no_bag"]


def test_consolidate_prefers_closer_dates_on_similar_price(cfg, store):
    far = evaluate(offer(700, out=date(2027, 1, 27), ret=date(2027, 3, 25)), store, cfg, NOW)
    near = evaluate(offer(720, out=date(2027, 2, 1), ret=date(2027, 4, 1)), store, cfg, NOW)
    ranked = consolidate([far, near], cfg)
    assert ranked[0].offer.out_date == date(2027, 2, 1)


# ---------------------------------------------------------------- planner
def test_weights_and_rotation(cfg, store):
    assert pair_weight(cfg, date(2027, 2, 1), date(2027, 4, 1)) == 1.0
    assert pair_weight(cfg, date(2027, 1, 27), date(2027, 3, 25)) < 0.05
    first = pick_pairs(cfg, store.state, NOW, 1)[0]
    assert first == (date(2027, 2, 1), date(2027, 4, 1))
    store.state["serpapi"]["pair_last_scan"]["2027-02-01|2027-04-01"] = iso(NOW)
    assert pick_pairs(cfg, store.state, NOW, 1)[0] != first


def test_quota_simulation_month(cfg, store):
    """Simulate 30 days of 2-hourly runs: stays under budget, preferred dates scanned most."""
    state = store.state
    state["serpapi"]["last_accrual"] = iso(NOW)
    counts: dict[str, int] = {}
    used = 0
    t = NOW
    for _ in range(30 * 12):
        t += timedelta(hours=2)
        n = int(accrue_credits(cfg, state, t)) - 0
        for o, r in pick_pairs(cfg, state, t, n):
            k = f"{o}|{r}"
            counts[k] = counts.get(k, 0) + 1
            state["serpapi"]["pair_last_scan"][k] = iso(t)
            used += 1
        state["serpapi"]["credits"] -= n
    assert used <= 250
    assert counts["2027-02-01|2027-04-01"] == max(counts.values())
    assert counts["2027-02-01|2027-04-01"] >= 15


def test_intervals_and_summary(cfg, store):
    assert scan_interval_minutes(cfg, date(2026, 11, 24)) == 120
    assert scan_interval_minutes(cfg, date(2027, 1, 1)) == 60
    assert scan_interval_minutes(cfg, date(2027, 1, 20)) == 30
    lima_8am = datetime(2026, 11, 25, 13, 5, tzinfo=timezone.utc)
    assert summary_due(cfg, store.state, lima_8am)
    assert not summary_due(cfg, store.state, lima_8am - timedelta(hours=1))
    store.state["last_summary_date"] = "2026-11-25"
    assert not summary_due(cfg, store.state, lima_8am)


# ---------------------------------------------------------------- sources
def test_serpapi_parsing_filters_two_stops(cfg):
    gf = GoogleFlights(FixtureHttp(), cfg, "k")
    offers, meta = gf.search_pair(date(2027, 2, 1), date(2027, 4, 1))
    assert [o.price for o in offers] == [742, 880, 958]          # 2-stop $610 dropped
    assert meta["price_insights"]["typical_price_range"] == [850, 1150]
    la = offers[0]
    gf.enrich(la)
    assert la.stops_ret == 1 and la.baggage.carry_on == "included" and la.baggage.checked_fee == 70
    assert "booking" in la.link and gf.searches_used == 3


def test_travelpayouts_filters(cfg):
    offers = AviasalesCache(FixtureHttp(), cfg, "t").fetch()
    assert sorted(o.price for o in offers) == [705, 990]           # outside window + 2 stops dropped
    assert all(o.cached and not o.baggage.known() for o in offers)
    assert offers[0].link.startswith("https://www.aviasales.com/search/")


def test_feed_matching(cfg):
    deals = DealFeeds(FixtureHttp(), cfg).fetch(datetime.now(timezone.utc))
    titles = [d.title for d in deals]
    assert len(deals) == 2 and any("Madrid" in t for t in titles)
    assert any(d.error_fare and "Lisbon" in d.title for d in deals)
    assert parse_feed("<rss><channel></channel></rss>") == []


# ---------------------------------------------------------------- end to end
class Recorder(EmailNotifier):
    def __init__(self, cfg=None):
        super().__init__(cfg or {}, "me@gmail.com", "abcd efgh ijkl mnop", dry_run=True)
        self.sent: list[str] = []

    def send(self, text, buttons=None, subject=None):
        self.sent.append(text)
        return True


def run(cfg, store, http, now):
    tg = Recorder(cfg)
    ctx = agent_main.ScanContext(cfg, Secrets(None, None, None, "k", "t"), http, tg, store, now)
    agent_main.run_scan(ctx)
    return tg.sent


def test_end_to_end_no_repeats(cfg, store):
    sent1 = run(cfg, store, FixtureHttp(), NOW)
    assert sum("Flight deal" in s for s in sent1) >= 1
    assert sum("Deal blog" in s or "deal blog" in s for s in sent1) == 2
    sent2 = run(cfg, store, FixtureHttp(), NOW + timedelta(hours=2))
    assert not any("Flight deal" in s or "blog" in s for s in sent2), sent2   # nothing repeated


def test_failing_source_does_not_stop_others(cfg, store):
    http = FixtureHttp(fail_hosts={"api.travelpayouts.com", "www.secretflying.com"})
    sent = run(cfg, store, http, NOW)
    assert any("Flight deal" in s for s in sent)                  # Google still worked
    assert store.state["sources"]["aviasales_cache"]["fail_streak"] == 1
    assert store.state["sources"]["deal_feeds"]["fail_streak"] == 1


def test_cached_deal_waits_for_live_check(cfg, store):
    c = copy.deepcopy(cfg)
    c["sources"]["serpapi"]["enabled"] = True
    c["sources"]["feeds"]["enabled"] = False
    store.state["serpapi"]["credits"] = 0           # no Google budget this run
    sent = run(c, store, FixtureHttp(), NOW)
    assert not any("Aviasales" in s for s in sent)              # $705 cached fare queued, not alerted
    assert "2027-01-30|2027-04-03" in store.state["verify_queue"]


def test_email_failure_is_recorded(monkeypatch, cfg):
    import smtplib
    n = EmailNotifier(cfg, "me@gmail.com", "secretpw", dry_run=False, attempts=2, base_delay=0)

    def boom(*a, **k):
        raise smtplib.SMTPServerDisconnected("down")
    monkeypatch.setattr(smtplib, "SMTP_SSL", boom)
    assert n.send("<b>hi</b>") is False
    assert n.failures and "secretpw" not in n.failures[0]


def test_email_auth_error_not_retried(monkeypatch, cfg):
    import smtplib
    calls = []

    def bad_login(*a, **k):
        calls.append(1)
        raise smtplib.SMTPAuthenticationError(535, b"bad")
    monkeypatch.setattr(smtplib, "SMTP_SSL", bad_login)
    n = EmailNotifier(cfg, "me@gmail.com", "x", dry_run=False, attempts=4, base_delay=0)
    assert n.send("hi") is False and len(calls) == 1 and "app password" in n.failures[0]


def test_email_building(cfg):
    n = EmailNotifier(cfg, "me@gmail.com", "pw")
    msg = n._build("<b>✈️ Flight deal: LIM ⇄ MAD $742</b>\nline &amp; more",
                   [[{"text": "Open", "url": "https://x"}], [{"text": "Pause", "callback_data": "pause"}]], None)
    assert msg["Subject"] == "[Flights] ✈️ Flight deal: LIM ⇄ MAD $742"
    html = msg.get_body(("html",)).get_content()
    assert "mailto:me@gmail.com?subject=flights%20pause" in html and "https://x" in html
    assert "line & more" in msg.get_body(("plain",)).get_content()


@pytest.mark.parametrize("subject,cmd", [
    ("flights status", "status"), ("Flights PAUSE", "pause"), ("Re: flights resume", "resume"),
    ("vuelos pausa", "pause"), ("vuelos estado", "status"), ("flight help please", "help"),
    ("[Flights] ✈️ Flight deal: LIM ⇄ MAD $742", None), ("my flights pause", None), ("", None),
])
def test_parse_command(subject, cmd):
    assert parse_command(subject) == cmd


def test_process_commands(cfg, store):
    n = Recorder(cfg)
    n.read_commands = lambda state: ["pause", "status"]
    assert agent_main.process_commands(n, store) is True
    assert store.state["paused"] and "Paused" in n.sent[0]
    n.read_commands = lambda state: []
    agent_main.process_commands(n, store, ["resume"])
    assert not store.state["paused"] and store.state["last_scan"] is None


class FakeIMAP:
    """Mimics the parts of imaplib.IMAP4_SSL the agent uses."""
    messages = {
        10: ("me@gmail.com", "[Flights] ✈️ Flight deal: LIM ⇄ MAD $742"),
        11: ("me@gmail.com", "flights pause"),
        12: ("stranger@evil.com", "flights resume"),
        13: ("Me@Gmail.com", "Re: vuelos estado"),
    }

    def __init__(self, *a, **k):
        self.untagged_responses = {"UIDVALIDITY": [b"777"]}
        self.flagged = []

    def login(self, *a):
        return "OK", []

    def select(self, *a):
        return "OK", [b"4"]

    def uid(self, cmd, *args):
        if cmd == "search":
            return "OK", [b" ".join(str(u).encode() for u in self.messages)]
        if cmd == "fetch":
            frm, subj = self.messages[int(args[0])]
            hdr = f"From: {frm}\r\nSubject: {subj}\r\n\r\n".encode()
            return "OK", [(b"x", hdr)]
        if cmd == "store":
            self.flagged.append(args[0])
            return "OK", []

    def logout(self):
        pass


def test_read_command_emails(monkeypatch, cfg):
    import imaplib
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    n = EmailNotifier(cfg, "me@gmail.com", "pw")
    state = {"email_last_uid": None, "email_uidvalidity": None}
    assert n.read_commands(state) == []                 # first run: remember position, act on nothing
    assert state["email_last_uid"] == 13
    state["email_last_uid"] = 9                         # pretend the emails are new
    assert n.read_commands(state) == ["pause", "status"]   # stranger ignored, alert subject ignored
    assert n.read_commands(state) == []                 # not processed twice


def test_send_to_other_addresses(cfg):
    n = EmailNotifier(cfg, "agent@gmail.com", "pw", to="mary@hotmail.com, other@yahoo.com")
    msg = n._build("<b>hi</b>", [[{"text": "Pause", "callback_data": "pause"}]], None)
    assert msg["To"] == "mary@hotmail.com, other@yahoo.com" and msg["From"].endswith("<agent@gmail.com>")
    # the Pause button writes to the agent's inbox, and a command sent from mary@hotmail.com is accepted
    assert "mailto:agent@gmail.com" in msg.get_body(("html",)).get_content()
    assert "mary@hotmail.com" in n.to_list


def test_alternative_destination_and_link(cfg, store):
    http = FixtureHttp()
    gf = GoogleFlights(http, cfg, "k")
    offers, _ = gf.search_pair(date(2027, 2, 1), date(2027, 4, 1))
    assert http.calls[-1][1]["arrival_id"] == "MAD,BCN"          # one search covers both cities
    assert {o.airline: o.destination for o in offers}["Air Europa"] == "BCN"
    assert {o.airline: o.destination for o in offers}["LATAM"] == "MAD"
    from agent import messages as M
    ux = next(o for o in offers if o.airline == "Air Europa")
    text = M.format_alert(evaluate(ux, store, cfg, NOW), cfg)
    assert "LIM ⇄ BCN" in text and "Flight link:" in text and "https://www.google.com/travel/flights" in text


def test_link_fallback_when_missing(cfg, store):
    from agent import messages as M
    o = offer(700)
    o.link = ""
    o.destination = "BCN"
    text = M.format_alert(evaluate(o, store, cfg, NOW), cfg)
    assert "travel/flights?q=Flights%20from%20LIM%20to%20BCN%20on%202027-02-01%20through%202027-04-01" in text
