"""State + price history in one JSON file (data/state.json), committed back to the repo.

Why JSON and not SQLite: the file is small (well under 1 MB), GitHub shows readable
diffs of every run, you can open/edit it in the browser, and git never has to merge
a binary file. SQLite would be overkill for a few thousand price points.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATE_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "paused": False,
        "email_last_uid": None,
        "email_uidvalidity": None,
        "last_run": None,
        "last_scan": None,
        "last_summary_date": None,
        "serpapi": {"credits": 3.0, "last_accrual": None, "searches_left": None, "pair_last_scan": {}},
        "history": {},
        "latest": {},
        "daily_min": {},
        "alerts_sent": {},
        "feed_seen": {},
        "verify_queue": {},
        "google_typical": None,
        "sources": {},
        "enrich_cache": {},
    }


class Store:
    def __init__(self, path: str | Path, points_per_key: int = 30):
        self.path = Path(path)
        self.points_per_key = points_per_key
        self.state = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            log.info("No state file yet at %s, starting fresh", self.path)
            return empty_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            backup = self.path.with_suffix(".corrupt.json")
            log.error("State file unreadable (%s). Moving it to %s and starting fresh.", e, backup)
            self.path.replace(backup)
            return empty_state()
        base = empty_state()
        base.update(data)
        for k, v in empty_state()["serpapi"].items():
            base["serpapi"].setdefault(k, v)
        return base

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prune()
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=1, sort_keys=True, default=str)
        os.replace(tmp, self.path)

    # ---- price history -------------------------------------------------
    def last_observation(self, key: str, source: str | None = None) -> list | None:
        for obs in reversed(self.state["history"].get(key, [])):
            if source is None or obs[2] == source:
                return obs
        return None

    def add_observation(self, key: str, price: float, source: str, now: datetime) -> None:
        """Keep a point when the price changed or 12h passed (keeps the file small)."""
        hist = self.state["history"].setdefault(key, [])
        last = self.last_observation(key, source)
        if last is not None:
            age = now - (parse_iso(last[0]) or now)
            if abs(last[1] - price) < 0.5 and age < timedelta(hours=12):
                return
        hist.append([iso(now), round(price, 2), source])
        if len(hist) > self.points_per_key:
            del hist[: len(hist) - self.points_per_key]

        tier = key.rsplit("|", 1)[1]
        day = now.date().isoformat()
        dm = self.state["daily_min"].setdefault(tier, {})
        dm[day] = round(min(price, dm.get(day, price)), 2)

    def tier_median(self, tier: str, window_days: int, now: datetime) -> tuple[float | None, int]:
        """Median of the tier's prices in the window, one point per date pair per day."""
        cutoff = now - timedelta(days=window_days)
        per_day: dict[tuple[str, str], float] = {}
        for key, hist in self.state["history"].items():
            if not key.endswith("|" + tier):
                continue
            for ts, price, _src in hist:
                t = parse_iso(ts)
                if t and t >= cutoff:
                    k = (key, t.date().isoformat())
                    per_day[k] = min(price, per_day.get(k, price))
        vals = list(per_day.values())
        if not vals:
            return None, 0
        return statistics.median(vals), len(vals)

    # ---- housekeeping --------------------------------------------------
    def _prune(self) -> None:
        now = utcnow()
        cutoff_day = (now - timedelta(days=90)).date().isoformat()
        for dm in self.state["daily_min"].values():
            for d in [d for d in dm if d < cutoff_day]:
                del dm[d]
        week_ago = now - timedelta(days=14)
        for k in [k for k, ts in self.state["feed_seen"].items() if (parse_iso(ts) or now) < week_ago]:
            del self.state["feed_seen"][k]
        for k in [k for k, ts in self.state["enrich_cache"].items()
                  if (parse_iso(ts.get("ts") if isinstance(ts, dict) else ts) or now) < now - timedelta(days=3)]:
            del self.state["enrich_cache"][k]

    # ---- source health ---------------------------------------------------
    def source_ok(self, name: str, now: datetime) -> None:
        s = self.state["sources"].setdefault(name, {})
        s.update({"last_ok": iso(now), "fail_streak": 0, "warned": False})

    def source_failed(self, name: str, err: str, now: datetime) -> int:
        s = self.state["sources"].setdefault(name, {})
        s["last_error"] = iso(now)
        s["error"] = err[:300]
        s["fail_streak"] = int(s.get("fail_streak", 0)) + 1
        return s["fail_streak"]
