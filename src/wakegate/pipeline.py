from __future__ import annotations

import time
from dataclasses import dataclass

from .layers import Cost, Layer, LayerContext, LayerResult, LayerRun


@dataclass
class PipelineOutcome:
    #: The result that decided. Always wake, skip or batch.
    final: LayerResult
    #: Name of the layer that produced it.
    decided_by: str | None
    runs: list[LayerRun]


def run_pipeline(layers: list[Layer], ctx: LayerContext) -> PipelineOutcome:
    """Run layers in order until one returns a final result.

    - A `defer` passes to the next layer.
    - Governors only run while the current proposal is a non-final wake.
    - A layer that raises fails open: the event becomes a wake proposal (or a skip if the layer
      says `on_error = "skip"`), the error is recorded, and the cascade continues.
    """
    runs: list[LayerRun] = []
    current: LayerResult | None = None
    decided_by: str | None = None

    for layer in layers:
        if layer.kind == "governor" and not (current and current.outcome == "wake" and not current.final):
            continue
        ctx.proposal = current
        started = time.perf_counter()
        try:
            result = layer.run(ctx)
        except Exception as e:  # noqa: BLE001 - any failure of a layer must fail open
            result = LayerResult("error", False, f"error:{layer.name}", detail={"type": type(e).__name__, "message": str(e)[:200]})
        ms = (time.perf_counter() - started) * 1000
        result.cost = Cost(ms=round(ms, 3), tokens_in=result.cost.tokens_in, tokens_out=result.cost.tokens_out, usd=result.cost.usd)
        runs.append(LayerRun(len(runs), layer.name, layer.kind, "enforce", True, result, ms))

        if result.outcome == "defer":
            continue
        if result.outcome == "error":
            if layer.on_error == "skip":
                current, decided_by = LayerResult("skip", True, f"error:{layer.name}"), layer.name
                break
            if current is None:  # fail open
                current, decided_by = LayerResult("wake", False, f"error:{layer.name}"), layer.name
            continue
        current, decided_by = result, layer.name
        if result.final:
            break

    if current is None:
        current = LayerResult("wake", False, "default")
    return PipelineOutcome(current, decided_by, runs)
