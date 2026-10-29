import pytest

from wakegate import Gate, Store, WakeEvent, parse_config

T0 = 1_767_614_400_000  # 2026-01-05 12:00:00 UTC
MIN = 60_000


@pytest.fixture
def setup():
    stores = []

    def make(yaml: str = "version: 1"):
        store = Store(":memory:")
        stores.append(store)
        return Gate(parse_config(yaml), store), store

    yield make
    for s in stores:
        s.close()


def ev(source: str, key: str, ts: int, data=None, severity: str | None = None) -> WakeEvent:
    return WakeEvent(source, key, ts, severity, data)
