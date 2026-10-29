from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Action = Literal["wake", "skip", "batch"]
Mode = Literal["enforce", "shadow", "canary"]
"""enforce: the verdict is delivered.
shadow:  every event is delivered as a wake; the verdict is only recorded.
canary:  a sampled event that would have been skipped is delivered as a wake instead."""


@dataclass
class WakeEvent:
    """The standard envelope every source adapter produces."""

    source: str
    key: str
    ts: int | None = None
    severity: str | None = None
    data: Any = None

    @classmethod
    def from_dict(cls, raw: Any) -> WakeEvent:
        if not isinstance(raw, dict) or not isinstance(raw.get("source"), str) or not isinstance(raw.get("key"), str):
            raise ValueError('event needs string "source" and "key"')
        ts = raw.get("ts")
        if ts is not None and (isinstance(ts, bool) or not isinstance(ts, (int, float))):
            raise ValueError('"ts" must be epoch milliseconds')
        return cls(raw["source"], raw["key"], None if ts is None else int(ts), raw.get("severity"), raw.get("data"))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"source": self.source, "key": self.key}
        if self.ts is not None:
            out["ts"] = self.ts
        if self.severity is not None:
            out["severity"] = self.severity
        if self.data is not None:
            out["data"] = self.data
        return out


@dataclass
class Decision:
    id: int
    source: str
    key: str
    ts: int
    fingerprint: str
    changed: bool
    #: What the caller should do. In shadow mode and for canaries this is always "wake".
    action: Action
    #: What the rules decided. Equals `action` unless mode is shadow or canary.
    verdict: Action
    mode: Mode
    #: Machine-readable reason for the verdict, e.g. "duplicate", "bypass:critical", "daily-budget".
    reason: str
    rule: str | None = None
    #: Set on canaries: tells whoever receives the wake how to record whether it was worth it.
    canary: bool = False
    note: str | None = None
    #: On a wake with flush_on_wake: events batched earlier, delivered now (once).
    batch: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON shape shared by the CLI and the MCP tools (camelCase keys are part of the contract)."""
        out: dict[str, Any] = {
            "action": self.action,
            "verdict": self.verdict,
            "mode": self.mode,
            "reason": self.reason,
        }
        if self.rule is not None:
            out["rule"] = self.rule
        out.update(source=self.source, key=self.key, ts=self.ts, fingerprint=self.fingerprint, changed=self.changed, id=self.id)
        if self.canary:
            out["canary"] = True
            out["note"] = self.note
        if self.batch:
            out["batch"] = self.batch
        return out
