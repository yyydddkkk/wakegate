from __future__ import annotations

import time
from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: int) -> str:
    """Like JavaScript's toISOString(): UTC, millisecond precision, trailing Z."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@lru_cache(maxsize=32)
def _zone(tz: str) -> ZoneInfo:
    return ZoneInfo(tz)


def _local(ts: int, tz: str | None) -> datetime:
    if tz:
        return datetime.fromtimestamp(ts / 1000, tz=_zone(tz))
    return datetime.fromtimestamp(ts / 1000).astimezone()  # the system zone


def day_of(ts: int, tz: str | None = None) -> str:
    return _local(ts, tz).strftime("%Y-%m-%d")


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_quiet_hours(ts: int, start: str, end: str, tz: str | None = None) -> bool:
    """The window may wrap midnight, e.g. 22:00-07:00. start == end means never."""
    s, e = _minutes(start), _minutes(end)
    if s == e:
        return False
    d = _local(ts, tz)
    now = d.hour * 60 + d.minute
    return s <= now < e if s < e else (now >= s or now < e)
