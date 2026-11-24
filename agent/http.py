"""HTTP helper: one requests.Session, retries with exponential backoff, secret redaction."""
from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

USER_AGENT = "flight-deal-agent/1.0 (personal price monitor; +https://github.com)"
RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class Http:
    def __init__(self, secrets: list[str] | None = None, attempts: int = 3, base_delay: float = 2.0,
                 timeout: float = 45.0):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
        self.secrets = [s for s in (secrets or []) if s]
        self.attempts = attempts
        self.base_delay = base_delay
        self.timeout = timeout

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def request(self, method: str, url: str, *, params: dict | None = None, json_body: Any = None,
                headers: dict | None = None, attempts: int | None = None, timeout: float | None = None
                ) -> requests.Response:
        attempts = attempts or self.attempts
        last_err: Exception | None = None
        for i in range(1, attempts + 1):
            try:
                resp = self.session.request(method, url, params=params, json=json_body, headers=headers,
                                            timeout=timeout or self.timeout)
            except requests.RequestException as e:
                last_err = HttpError(self.redact(f"{type(e).__name__}: {e}"))
                log.warning("Request to %s failed (attempt %d/%d): %s", _host(url), i, attempts, last_err)
            else:
                if resp.status_code < 400:
                    return resp
                body = self.redact(resp.text[:500])
                last_err = HttpError(f"HTTP {resp.status_code} from {_host(url)}: {body}", resp.status_code, body)
                if resp.status_code not in RETRY_STATUS:
                    raise last_err
                log.warning("HTTP %s from %s (attempt %d/%d)", resp.status_code, _host(url), i, attempts)
                retry_after = _retry_after(resp)
                if retry_after is not None and i < attempts:
                    time.sleep(min(retry_after, 60))
                    continue
            if i < attempts:
                delay = self.base_delay * (2 ** (i - 1)) + random.uniform(0, 1)
                time.sleep(delay)
        assert last_err is not None
        raise last_err

    def get_json(self, url: str, params: dict | None = None, **kw) -> Any:
        resp = self.request("GET", url, params=params, **kw)
        try:
            return resp.json()
        except ValueError as e:
            raise HttpError(f"Invalid JSON from {_host(url)}") from e

    def get_text(self, url: str, **kw) -> str:
        return self.request("GET", url, **kw).text


def _host(url: str) -> str:
    return url.split("/")[2] if "://" in url else url


def _retry_after(resp: requests.Response) -> float | None:
    v = resp.headers.get("Retry-After")
    if v and v.isdigit():
        return float(v)
    return None
