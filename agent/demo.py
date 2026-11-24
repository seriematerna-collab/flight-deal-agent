"""Offline demo + test double: answers HTTP calls from tests/fixtures instead of the internet."""
from __future__ import annotations

import json
from email.utils import format_datetime
from pathlib import Path

from .config import Secrets
from .http import Http, HttpError
from .storage import utcnow

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def demo_secrets() -> Secrets:
    return Secrets(email_address=None, email_app_password=None, email_to=None,
                   serpapi_key="demo-serpapi-key", travelpayouts_token="demo-tp-token")


class FixtureHttp(Http):
    def __init__(self, cfg: dict | None = None, fail_hosts: set[str] | None = None):
        super().__init__([])
        self.calls: list[tuple[str, dict]] = []
        self.fail_hosts = fail_hosts or set()

    def _load(self, name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    def get_json(self, url, params=None, **kw):
        params = params or {}
        self.calls.append((url, dict(params)))
        host = url.split("/")[2]
        if host in self.fail_hosts:
            raise HttpError(f"HTTP 503 from {host}: simulated outage", 503)
        if "account.json" in url:
            return {"total_searches_left": 200}
        if "serpapi.com" in host:
            if "booking_token" in params:
                return json.loads(self._load("serpapi_booking.json"))[params["booking_token"]]
            if "departure_token" in params:
                return json.loads(self._load("serpapi_return.json")).get(
                    params["departure_token"], {"search_metadata": {"status": "Success"}, "best_flights": []})
            return json.loads(self._load("serpapi_search.json"))
        if "travelpayouts.com" in host:
            if params.get("departure_at") == "2027-01" and params.get("return_at") == "2027-04":
                return json.loads(self._load("tp_prices.json"))
            return {"success": True, "currency": "usd", "data": []}
        raise HttpError(f"no fixture for {url}")

    def get_text(self, url, **kw):
        self.calls.append((url, {}))
        host = url.split("/")[2]
        if host in self.fail_hosts:
            raise HttpError(f"HTTP 403 from {host}: simulated block", 403)
        return self._load("feed.xml").replace("{NOW_RFC822}", format_datetime(utcnow()))
