from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .config import Config
from .hashing import canonical_json, omit_paths, sha256
from .store import Store, brief
from .timeutil import day_of, in_quiet_hours, now_ms
from .types import Action, Decision, Mode, WakeEvent


@dataclass
class Verdict:
    action: Action
    reason: str
    rule: str | None = None


class Gate:
    """Decision order, first hit wins:

      1. duplicate     same fingerprint as last time for this key  -> skip
      2. bypass        matches a bypass rule                       -> wake (ignores 4 and 5)
      3. rules         first matching rule, else the default
      4. cooldown      woke for this key too recently              -> batch
      5. quiet hours / daily budget                                -> batch or skip

    A matching rule can list `exempt: [cooldown, quietHours, budget]` to skip some of 4 and 5.
    On a wake with flush_on_wake (default), batched events ride along in `batch`.
    Every decision, including skips, is written to the audit log.
    """

    def __init__(self, config: Config, store: Store, now: Callable[[], int] = now_ms) -> None:
        self.config = config
        self.store = store
        self._now = now

    def decide(self, event: WakeEvent) -> Decision:
        c = self.config
        ts = event.ts if event.ts is not None else self._now()
        ev = WakeEvent(event.source, event.key, ts, event.severity, event.data)
        payload: dict = {"s": ev.severity}
        if ev.data is not None:
            payload["d"] = omit_paths(ev.data, c.ignore_paths)
        fingerprint = sha256(canonical_json(payload))
        tz = c.budget.timezone

        with self.store.transaction():
            prev = self.store.get_state(ev.source, ev.key)
            changed = prev is None or prev.fingerprint != fingerprint
            verdict = self._evaluate(ev, ts, changed, prev.last_wake_ts if prev else None)

            # What the caller is told to do can differ from the verdict (shadow, canary).
            mode: Mode = "enforce"
            delivered: Action = verdict.action
            if c.mode == "shadow":
                mode, delivered = "shadow", "wake"
            elif is_canary_candidate(verdict.action, verdict.reason) and _canary_roll(ev, fingerprint) < c.canary_rate:
                mode, delivered = "canary", "wake"

            carried = [brief(r) for r in self.store.take_pending()] if delivered == "wake" and c.flush_on_wake else []

            id_ = self.store.insert_decision(
                ts=ts, day=day_of(ts, tz), source=ev.source, key=ev.key, fingerprint=fingerprint,
                action=delivered, verdict=verdict.action, mode=mode, reason=verdict.reason, rule=verdict.rule,
                changed=changed, event=ev.to_dict(),
            )
            # Cooldown and budget follow the verdict, so shadow mode simulates enforcement faithfully
            # and a canary does not spend the budget.
            self.store.put_state(ev.source, ev.key, fingerprint, ts, verdict.action == "wake")
            if delivered == "batch":
                self.store.add_pending(id_)

        note = None
        if mode == "canary":
            note = (
                f"Released as a canary: your rules would have skipped this ({verdict.reason}). "
                f"Record whether it was worth waking for: wakegate label {id_} wanted|unwanted."
            )
        return Decision(
            id=id_, source=ev.source, key=ev.key, ts=ts, fingerprint=fingerprint, changed=changed,
            action=delivered, verdict=verdict.action, mode=mode, reason=verdict.reason, rule=verdict.rule,
            canary=mode == "canary", note=note, batch=carried or None,
        )

    def _evaluate(self, ev: WakeEvent, ts: int, changed: bool, last_wake_ts: int | None) -> Verdict:
        c = self.config
        if c.dedupe and not changed:
            return Verdict("skip", "duplicate")

        view = ev.to_dict()
        bypass = next((r for r in c.bypass if r.when(view)), None)
        if bypass:
            return Verdict("wake", f"bypass:{bypass.name}", bypass.name)

        hit = next((r for r in c.rules if r.when(view)), None)
        exempt = set(hit.exempt) if hit else set()
        proposed = Verdict(hit.then, f"rule:{hit.name}", hit.name) if hit else Verdict(c.default_action, "default")
        if proposed.action != "wake":
            return proposed

        if "cooldown" not in exempt and c.cooldown_seconds > 0 and last_wake_ts is not None and ts - last_wake_ts < c.cooldown_seconds * 1000:
            return Verdict("batch", "cooldown", proposed.rule)
        b = c.budget
        if "quietHours" not in exempt and b.quiet_hours and in_quiet_hours(ts, b.quiet_hours[0], b.quiet_hours[1], b.timezone):
            return Verdict("batch", "quiet-hours", proposed.rule)
        if "budget" not in exempt and b.max_wakes_per_day is not None and self.store.count_wakes(day_of(ts, b.timezone)) >= b.max_wakes_per_day:
            return Verdict(b.over_budget, "daily-budget", proposed.rule)  # type: ignore[arg-type]
        return proposed


def is_canary_candidate(action: str, reason: str) -> bool:
    """A skip that carries information. Duplicates are excluded: they add nothing worth a wake."""
    return action == "skip" and reason != "duplicate"


def _canary_roll(ev: WakeEvent, fingerprint: str) -> float:
    """Deterministic value in [0, 1) per event, so replays and tests are reproducible."""
    h = sha256(f"{ev.source}\u0000{ev.key}\u0000{fingerprint}\u0000{ev.ts}")
    return int(h[:8], 16) / 0x1_0000_0000
