"""The built-in layers. Together they are exactly the decision order documented in the README:

  dedupe -> bypass -> rules -> cooldown -> quiet_hours -> budget

Layers that would never act under the current config are left out of the pipeline, so a trace
only contains layers that could have mattered.
"""

from __future__ import annotations

from .config import Config
from .layers import Layer, LayerContext, LayerResult
from .timeutil import day_of, in_quiet_hours


class DedupeLayer(Layer):
    name, kind = "dedupe", "detector"

    def run(self, ctx: LayerContext) -> LayerResult:
        detail = {"fingerprint": ctx.fingerprint, "previous": ctx.prev.fingerprint if ctx.prev else None}
        if not ctx.changed:
            return LayerResult("skip", True, "duplicate", detail=detail)
        return LayerResult("defer", False, "changed" if ctx.prev else "first-seen", detail=detail)


class BypassLayer(Layer):
    name, kind = "bypass", "rules"

    def run(self, ctx: LayerContext) -> LayerResult:
        rules = ctx.config.bypass
        for i, r in enumerate(rules):
            if r.when(ctx.view):
                return LayerResult("wake", True, f"bypass:{r.name}", r.name, detail={"evaluated": i + 1, "matched_index": i})
        return LayerResult("defer", False, "no-match", detail={"evaluated": len(rules), "matched_index": None})


class RulesLayer(Layer):
    name, kind = "rules", "rules"

    def run(self, ctx: LayerContext) -> LayerResult:
        c = ctx.config
        for i, r in enumerate(c.rules):
            if r.when(ctx.view):
                detail = {"evaluated": i + 1, "matched_index": i}
                if r.exempt:
                    detail["exempt"] = list(r.exempt)
                # A skip or batch is final. A wake is a proposal that the governors may still limit.
                return LayerResult(r.then, r.then != "wake", f"rule:{r.name}", r.name, detail=detail)
        detail = {"evaluated": len(c.rules), "matched_index": None}
        return LayerResult(c.default_action, c.default_action != "wake", "default", detail=detail)


class _Governor(Layer):
    kind = "governor"
    #: Name used in a rule's `exempt` list.
    exempt_key: str

    def exempted(self, ctx: LayerContext) -> bool:
        return bool(ctx.proposal and self.exempt_key in ctx.proposal.detail.get("exempt", ()))

    def block(self, ctx: LayerContext, outcome: str, reason: str, detail: dict) -> LayerResult:
        # The blocked event keeps the rule that proposed the wake, so reports can attribute it.
        return LayerResult(outcome, True, reason, ctx.proposal.rule if ctx.proposal else None, detail=detail)  # type: ignore[arg-type]

    def let_through(self, reason: str, detail: dict) -> LayerResult:
        return LayerResult("defer", False, reason, detail=detail)


class CooldownLayer(_Governor):
    name, exempt_key = "cooldown", "cooldown"

    def run(self, ctx: LayerContext) -> LayerResult:
        window_ms = ctx.config.cooldown_seconds * 1000
        last = ctx.prev.last_wake_ts if ctx.prev else None
        detail = {"window_ms": window_ms, "since_last_wake_ms": None if last is None else ctx.ts - last}
        if self.exempted(ctx):
            return self.let_through("exempt", detail)
        if last is not None and ctx.ts - last < window_ms:
            return self.block(ctx, "batch", "cooldown", detail)
        return self.let_through("ok", detail)


class QuietHoursLayer(_Governor):
    name, exempt_key = "quiet_hours", "quietHours"

    def run(self, ctx: LayerContext) -> LayerResult:
        b = ctx.config.budget
        start, end = b.quiet_hours  # type: ignore[misc]
        detail = {"window": f"{start}-{end}", "timezone": b.timezone}
        if self.exempted(ctx):
            return self.let_through("exempt", detail)
        if in_quiet_hours(ctx.ts, start, end, b.timezone):
            return self.block(ctx, "batch", "quiet-hours", detail)
        return self.let_through("ok", detail)


class BudgetLayer(_Governor):
    name, exempt_key = "budget", "budget"

    def run(self, ctx: LayerContext) -> LayerResult:
        b = ctx.config.budget
        used = ctx.store.count_wakes(day_of(ctx.ts, b.timezone))
        detail = {"used": used, "max": b.max_wakes_per_day}
        if self.exempted(ctx):
            return self.let_through("exempt", detail)
        if used >= b.max_wakes_per_day:  # type: ignore[operator]
            return self.block(ctx, b.over_budget, "daily-budget", detail)
        return self.let_through("ok", detail)


def default_layers(c: Config) -> list[Layer]:
    layers: list[Layer] = []
    if c.dedupe:
        layers.append(DedupeLayer())
    if c.bypass:
        layers.append(BypassLayer())
    layers.append(RulesLayer())
    if c.cooldown_seconds > 0:
        layers.append(CooldownLayer())
    if c.budget.quiet_hours:
        layers.append(QuietHoursLayer())
    if c.budget.max_wakes_per_day is not None:
        layers.append(BudgetLayer())
    return layers
