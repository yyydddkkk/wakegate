"""The layer contract: what a layer receives and what it returns.

A layer takes the event plus what earlier layers produced and returns a LayerResult. The
cascade (see pipeline.py) stops at the first `final` result. Governors only act on a
non-final wake proposal. Every run is recorded, which is what makes layers measurable one by one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .config import Config
    from .store import KeyState, Store
    from .types import Action, WakeEvent

Outcome = Literal["skip", "wake", "batch", "defer", "error"]
LayerKind = Literal["detector", "rules", "classifier", "governor"]
LayerMode = Literal["enforce", "shadow", "canary"]
#: What a configured layer may be set to. "off" removes it from the pipeline; "canary" is a decision-level mode.
ConfigurableMode = Literal["off", "shadow", "enforce"]


@dataclass(frozen=True)
class Cost:
    ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    usd: float | None = None


@dataclass
class LayerResult:
    #: skip / wake / batch decide. defer means "no confident answer, ask the next layer".
    outcome: Outcome
    #: True: nothing later may change this. False: a proposal that governors may still turn into batch/skip.
    final: bool = False
    #: Machine-readable ("duplicate", "rule:ignore-bots", "cooldown", ...).
    reason: str = ""
    rule: str | None = None
    #: 0..1, for classifiers.
    confidence: float | None = None
    #: Layer-specific explanation. Small and JSON-serialisable.
    detail: dict[str, Any] = field(default_factory=dict)
    #: Named derived data for later layers (unused until a layer needs it).
    views: dict[str, Any] = field(default_factory=dict)
    cost: Cost = field(default_factory=Cost)


@dataclass
class LayerContext:
    event: WakeEvent
    #: event as a plain dict: what conditions are evaluated against.
    view: dict[str, Any]
    ts: int
    fingerprint: str
    changed: bool
    prev: KeyState | None
    config: Config
    store: Store
    #: The current non-final wake proposal, if any. Governors read its `exempt` list.
    proposal: LayerResult | None = None


class Layer(ABC):
    name: str
    kind: LayerKind
    #: What to do if the layer raises: "wake" fails open (a broken filter costs money, it does not drop events).
    on_error: Action = "wake"
    #: "enforce": the outcome is applied. "shadow": it runs and is recorded, and the cascade proceeds
    #: as if it had deferred. Set from the config; "off" layers are not in the pipeline at all.
    mode: Literal["enforce", "shadow"] = "enforce"

    @classmethod
    def create(cls, name: str, params: dict[str, Any]) -> Layer:
        """Build an instance from its `params`. Override to validate params and set up state."""
        if params:
            raise ValueError(f'layer "{name}" takes no params, got: {", ".join(params)}')
        return cls()

    @abstractmethod
    def run(self, ctx: LayerContext) -> LayerResult: ...


@dataclass
class LayerRun:
    """One executed layer, as recorded in the trace."""

    seq: int
    layer: str
    kind: LayerKind
    mode: LayerMode
    #: Whether this layer's outcome decided what the caller was told.
    applied: bool
    result: LayerResult
    duration_ms: float
