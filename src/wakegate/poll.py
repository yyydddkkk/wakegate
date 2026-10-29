from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from .hashing import sha256
from .store import Store
from .types import WakeEvent


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # lower-case names
    body: bytes = b""


Fetcher = Callable[[str, dict[str, str], float], HttpResponse]


def default_fetch(url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - the caller chose the URL
            return HttpResponse(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return HttpResponse(304, {k.lower(): v for k, v in e.headers.items()})
        raise


@dataclass
class PollResult:
    #: "not-modified" or "event"
    kind: str
    event: WakeEvent | None = None
    poll_interval_hint: float | None = None


def poll_url(url: str, store: Store, source: str | None = None, timeout: float = 15.0,
             user_agent: str = "wakegate/0.2", fetch: Fetcher = default_fetch) -> PollResult:
    """One conditional GET. Sends If-None-Match / If-Modified-Since from the last response, so
    unchanged resources cost a 304. The body is hashed and never stored or put in the event.

    A single poll is deliberately dumb: scheduling is the caller's job (cron, a timer). Honour
    `poll_interval_hint` (GitHub's x-poll-interval, Retry-After) when you choose the next run."""
    headers = {"user-agent": user_agent}
    prev = store.get_validators(url)
    if prev:
        if prev[0]:
            headers["if-none-match"] = prev[0]
        if prev[1]:
            headers["if-modified-since"] = prev[1]

    res = fetch(url, headers, timeout)
    raw_hint = res.headers.get("x-poll-interval") or res.headers.get("retry-after")
    try:
        hint = float(raw_hint) if raw_hint is not None else None
    except ValueError:
        hint = None

    if res.status == 304:
        return PollResult("not-modified", None, hint)

    store.put_validators(url, res.headers.get("etag"), res.headers.get("last-modified"))
    event = WakeEvent(
        source=source or urlparse(url).netloc,
        key=url,
        data={"status": res.status, "bodyHash": sha256(res.body.decode("latin-1")), "bytes": len(res.body)},
    )
    return PollResult("event", event, hint)
