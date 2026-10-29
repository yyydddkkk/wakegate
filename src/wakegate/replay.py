from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .gate import Gate
from .store import Store
from .types import WakeEvent


@dataclass
class ReplayResult:
    total: int
    counts: dict[str, int]
    reasons: dict[str, int]
    #: Estimated cost if every event woke the agent, vs. only the wakes (plus canaries).
    baseline_cost: float
    gated_cost: float
    #: Skips released as canaries (they cost a wake each and are included in gated_cost).
    canaries_released: int
    #: Batched events that rode along with a later wake (flush_on_wake).
    batch_delivered: int
    #: Batched events still waiting when the events ran out.
    batch_stranded: int


def read_events(path: str | Path) -> list[WakeEvent]:
    events: list[WakeEvent] = []
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            raise ValueError(f"{path}:{i}: invalid JSON") from None
        ev = WakeEvent.from_dict(raw)
        if ev.ts is None:
            raise ValueError(f'{path}: event on line {i} has no numeric "ts" (required for replay)')
        events.append(ev)
    return sorted(events, key=lambda e: e.ts or 0)


def replay(events: list[WakeEvent], config: Config, cost_per_wake: float = 0.0) -> ReplayResult:
    """Run events through the gate against a fresh in-memory state, using each event's own
    timestamp as the clock. Nothing on disk is touched."""
    store = Store(":memory:")
    try:
        gate = Gate(config, store)
        counts = {"wake": 0, "skip": 0, "batch": 0}
        reasons: dict[str, int] = {}
        delivered = canaries = 0
        for e in events:
            d = gate.decide(e)
            delivered += len(d.batch or [])
            if d.mode == "canary":
                canaries += 1
            # Count what the rules decided, so a shadow-mode config shows what enforcing would do.
            counts[d.verdict] += 1
            key = f"{d.verdict}:{d.reason}"
            reasons[key] = reasons.get(key, 0) + 1
        return ReplayResult(
            total=len(events), counts=counts, reasons=reasons,
            baseline_cost=len(events) * cost_per_wake, gated_cost=(counts["wake"] + canaries) * cost_per_wake,
            canaries_released=canaries, batch_delivered=delivered, batch_stranded=store.pending_count(),
        )
    finally:
        store.close()
