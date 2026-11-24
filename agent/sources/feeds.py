"""Deal blog feeds (RSS/Atom), e.g. Secret Flying, for error fares and flash sales.
Only reads the public feed; never scrapes airline sites.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from defusedxml import ElementTree as ET

from ..http import Http, HttpError
from ..models import FeedDeal
from . import SourceError

log = logging.getLogger(__name__)

ATOM = "{http://www.w3.org/2005/Atom}"
PRICE_RE = re.compile(r"(?:US\$|USD\s?\$?|\$)\s?(\d{2,5}(?:,\d{3})?)|(\d{2,5})\s?USD", re.I)


def _kw_match(text: str, keywords: list[str]) -> bool:
    for kw in keywords:
        if kw.isupper() and len(kw) <= 3:      # airport code: exact, case-sensitive
            if re.search(rf"\b{re.escape(kw)}\b", text):
                return True
        elif re.search(rf"\b{re.escape(kw)}\b", text, re.I):
            return True
    return False


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    items = []
    for it in root.iter("item"):
        items.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "date": it.findtext("pubDate"),
            "cats": [c.text or "" for c in it.findall("category")],
            "desc": it.findtext("description") or "",
        })
    for it in root.iter(f"{ATOM}entry"):
        link_el = it.find(f"{ATOM}link")
        items.append({
            "title": (it.findtext(f"{ATOM}title") or "").strip(),
            "link": link_el.get("href", "") if link_el is not None else "",
            "date": it.findtext(f"{ATOM}updated") or it.findtext(f"{ATOM}published"),
            "cats": [c.get("term", "") for c in it.findall(f"{ATOM}category")],
            "desc": it.findtext(f"{ATOM}summary") or "",
        })
    return items


class DealFeeds:
    name = "deal_feeds"

    def __init__(self, http: Http, cfg: dict):
        self.http = http
        self.fc = cfg["sources"]["feeds"]

    def match(self, item: dict, now: datetime) -> FeedDeal | None:
        fc = self.fc
        published = _parse_date(item["date"])
        if published and now - published > timedelta(days=int(fc.get("max_age_days", 3))):
            return None
        text = " ".join([item["title"], " ".join(item["cats"])])
        if not _kw_match(text, fc["origin_keywords"]):
            return None
        is_error = _kw_match(text + " " + item["desc"][:500], fc.get("error_fare_keywords") or [])
        dest = _kw_match(text, fc["destination_keywords"])
        europe = _kw_match(text, fc.get("europe_keywords") or [])
        if not (dest or (is_error and europe)):
            return None
        m = PRICE_RE.search(item["title"])
        price = None
        if m:
            price = float((m.group(1) or m.group(2)).replace(",", ""))
        return FeedDeal(item["title"], item["link"], published.isoformat() if published else "", price, is_error)

    def fetch(self, now: datetime) -> list[FeedDeal]:
        deals, errors = [], []
        urls = self.fc.get("urls") or []
        for url in urls:
            try:
                xml = self.http.get_text(url, attempts=2, timeout=30)
                items = parse_feed(xml)
            except (HttpError, ET.ParseError, ValueError) as e:
                errors.append(f"{url}: {e}")
                continue
            log.info("Feed %s: %d items", url, len(items))
            for it in items:
                d = self.match(it, now)
                if d and d.link:
                    deals.append(d)
        if urls and len(errors) == len(urls):
            raise SourceError("; ".join(errors)[:400])
        for e in errors:
            log.warning("Feed failed: %s", e)
        return deals
