from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .builtin_layers import default_layers
from .config import Config
from .hashing import canonical_json, omit_paths, sha256
from .layers import Layer, LayerContext
from .pipeline import run_pipeline
from .store import Store, brief
from .timeutil import day_of, now_ms
from .types import Action, Decision, Mode, WakeEvent


@dataclass
class Verdict:
    action: Action
    reason: str
    rule: str | None = None


class Gate:
    """Runs an event through the layers and records the decision and its trace.

    Layers, in order (see builtin_layers.py), first final result wins:

      dedupe        same fingerprint as last time for this key  -> skip
      bypass        matches a bypass rule                       -> wake (governors do not apply)
      rules         first matching rule, else the default
      cooldown      woke for this key too recently              -> batch
      quiet_hours / budget                                      -> batch or skip

    A matching rule can list `exempt: [cooldown, quietHours, budget]` to skip some governors.
    On a wake with flush_on_wake (default), batched events ride along in `batch`.
    Every decision, including skips, is written to the audit log with a record of each layer.
    """

    def __init__(self, config: Config, store: Store, now: Callable[[], int] = now_ms, layers: list[Layer] | None = None) -> None:
        self.config = config
        self.store = store
        self._now = now
        self.layers = layers if layers is not None else default_layers(config)

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
            ctx = LayerContext(ev, ev.to_dict(), ts, fingerprint, changed, prev, c, self.store)
            outcome = run_pipeline(self.layers, ctx)
            verdict = Verdict(outcome.final.outcome, outcome.final.reason, outcome.final.rule)  # type: ignore[arg-type]

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
            # How each layer's outcome relates to what was delivered: in shadow nothing was applied,
            # and for a canary the layer that skipped the event was overridden.
            for run in outcome.runs:
                if mode == "shadow":
                    run.mode, run.applied = "shadow", False
                elif mode == "canary" and run.layer == outcome.decided_by:
                    run.mode, run.applied = "canary", False
            self.store.insert_layer_runs(id_, outcome.decided_by, outcome.runs)
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


def is_canary_candidate(action: str, reason: str) -> bool:
    """A skip that carries information. Duplicates are excluded: they add nothing worth a wake."""
    return action == "skip" and reason != "duplicate"


def _canary_roll(ev: WakeEvent, fingerprint: str) -> float:
    """Deterministic value in [0, 1) per event, so replays and tests are reproducible."""
    h = sha256(f"{ev.source}\u0000{ev.key}\u0000{fingerprint}\u0000{ev.ts}")
    return int(h[:8], 16) / 0x1_0000_0000
