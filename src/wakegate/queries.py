from __future__ import annotations

import math
import re
from typing import Any

from .config import Config, describe_config
from .gate import is_canary_candidate
from .hashing import seeded_random
from .store import DecisionRow, Store, brief
from .timeutil import day_of, in_quiet_hours, iso, now_ms

__all__ = ["brief", "catchup", "parse_since", "report", "review", "rules_status", "wilson"]

_UNITS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}


def parse_since(v: str, now: int | None = None) -> int:
    """'24h', '30m', '7d', '90s' or an ISO date -> epoch ms."""
    now = now_ms() if now is None else now
    m = re.fullmatch(r"(\d+)([smhd])", v)
    if m:
        return now - int(m[1]) * _UNITS[m[2]]
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f'expected a duration like 24h or an ISO date, got "{v}"') from None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp() * 1000)


def catchup(store: Store, since_ms: int, include_filtered: bool = False) -> dict[str, Any]:
    """What changed while the agent was away. A by-product of the per-key state."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    filtered = 0
    for r in store.decisions_since(since_ms):
        if not r.changed:
            continue
        # A skip rule is the user saying "I don't care about this"; don't hand it back as news.
        if not include_filtered and r.verdict == "skip" and r.reason.startswith("rule:"):
            filtered += 1
            continue
        cur = by_key.get((r.source, r.key))
        by_key[(r.source, r.key)] = {
            "source": r.source, "key": r.key, "changes": (cur["changes"] if cur else 0) + 1,
            "lastChange": r.ts, "lastAction": r.verdict,
        }
    return {
        "since": iso(since_ms),
        "pendingBatch": store.pending_count(),
        "filteredBySkipRules": filtered,
        "changed": [{**v, "lastChange": iso(v["lastChange"])} for v in by_key.values()],
    }


def rules_status(config: Config, store: Store, now: int | None = None) -> dict[str, Any]:
    """The active policy plus where today stands against it."""
    now = now_ms() if now is None else now
    b = config.budget
    wakes_today = store.count_wakes(day_of(now, b.timezone))
    return {
        **describe_config(config),
        "now": {
            "time": iso(now),
            "inQuietHours": bool(b.quiet_hours) and in_quiet_hours(now, b.quiet_hours[0], b.quiet_hours[1], b.timezone),
            "wakesToday": wakes_today,
            "wakesRemainingToday": None if b.max_wakes_per_day is None else max(0, b.max_wakes_per_day - wakes_today),
            "pendingBatch": store.pending_count(),
        },
    }


def _is_suppressed(r: DecisionRow) -> bool:
    return is_canary_candidate(r.verdict, r.reason)


def wilson(k: int, n: int) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials."""
    if n == 0:
        return (0.0, 1.0)
    z = 1.96
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _round(x: float) -> int:
    return math.floor(x + 0.5)  # like JavaScript's Math.round


def _estimate(per_layer: dict[str, dict[str, int]]) -> dict[str, Any] | None:
    """The share of suppressed events that were wanted.

    Each layer is sampled uniformly (a canary, or a review draw), but different layers can be
    sampled at different rates, so pooling the samples would over-weight the heavily sampled
    layers. The estimate is therefore stratified by the layer that suppressed the event, each
    layer weighted by how many events it suppressed. A layer with no labelled sample is left out
    and listed under `uncovered`: its rate is not extrapolated from the others.

    One sampled layer: the Wilson interval for its sample. Several: a normal approximation to the
    stratified estimator, with a smoothed variance so a sample of all zeros is not read as certainty.
    """
    strata = {name: b for name, b in per_layer.items() if b["n"] > 0}
    if not strata:
        return None
    n_total = sum(b["n"] for b in strata.values())
    wanted_total = sum(b["wanted"] for b in strata.values())
    uncovered = sum(b["suppressed"] for name, b in per_layer.items() if name not in strata)

    bounds = {name: wilson(b["wanted"], b["n"]) for name, b in strata.items()}
    # Events not already delivered as canaries: the ones that would really have been lost.
    lo_events = sum(_round(bounds[name][0] * b["undelivered"]) for name, b in strata.items())
    hi_events = sum(_round(bounds[name][1] * b["undelivered"]) for name, b in strata.items())

    if len(strata) == 1:
        (name, b), = strata.items()
        rate, ci, method = b["wanted"] / b["n"], list(bounds[name]), "pooled"
    else:
        pop = sum(b["suppressed"] for b in strata.values())
        rate = sum(b["suppressed"] / pop * (b["wanted"] / b["n"]) for b in strata.values())
        var = 0.0
        for b in strata.values():
            p = (b["wanted"] + 0.5) / (b["n"] + 1)
            var += (b["suppressed"] / pop) ** 2 * p * (1 - p) / b["n"]
        half = 1.96 * math.sqrt(var)
        ci, method = [max(0.0, rate - half), min(1.0, rate + half)], "stratified"
    return {
        "sampleSize": n_total, "wanted": wanted_total, "rate": rate, "ci95": ci,
        "method": method, "layers": sorted(strata), "uncoveredSuppressed": uncovered,
        # Interval times the suppressed events not already delivered as canaries, summed over the layers sampled.
        # In shadow mode: events enforcing would have missed.
        "expectedMissedEvents": [lo_events, hi_events],
    }


def report(store: Store, since_ms: int, cost_per_wake: float | None = None, min_sample: int = 30) -> dict[str, Any]:
    """What the rules decided, and an estimate of how many suppressed events were wanted."""
    rows = store.decisions_since(since_ms)
    labels = store.labels()
    queued = set(store.queued_ids())
    verdicts = {"wake": 0, "skip": 0, "batch": 0}
    by_mode = {"enforce": 0, "shadow": 0, "canary": 0}
    reasons: dict[tuple[str, str], int] = {}
    per_rule: dict[str, dict[str, int]] = {}
    per_layer: dict[str, dict[str, int]] = {}
    suppressed = suppressed_undelivered = shadow_avoidable = unwanted_wakes = unlabeled = 0
    sample_n = sample_wanted = 0

    for r in rows:
        verdicts[r.verdict] += 1
        by_mode[r.mode] += 1
        reasons[(r.verdict, r.reason)] = reasons.get((r.verdict, r.reason), 0) + 1
        if r.mode == "shadow" and r.verdict != "wake":
            shadow_avoidable += 1
        lab = labels.get(r.id)
        if _is_suppressed(r):
            suppressed += 1
            if r.mode != "canary":
                suppressed_undelivered += 1
            is_sample = r.mode == "canary" or r.id in queued
            b = per_rule.setdefault(r.rule or r.reason, {"suppressed": 0, "n": 0, "wanted": 0})
            lb = per_layer.setdefault(r.decided_by or "unknown", {"suppressed": 0, "undelivered": 0, "n": 0, "wanted": 0})
            b["suppressed"] += 1
            lb["suppressed"] += 1
            if r.mode != "canary":
                lb["undelivered"] += 1
            if is_sample and not lab:
                unlabeled += 1
            if is_sample and lab and lab["sample"]:
                sample_n += 1
                b["n"] += 1
                lb["n"] += 1
                if lab["label"] == "wanted":
                    sample_wanted += 1
                    b["wanted"] += 1
                    lb["wanted"] += 1
        if lab and lab["label"] == "unwanted" and r.verdict == "wake":
            unwanted_wakes += 1

    labelled = {"total": 0, "wanted": 0, "unwanted": 0}
    for r in rows:
        lab = labels.get(r.id)
        if lab:
            labelled["total"] += 1
            labelled[lab["label"]] += 1

    miss = _estimate(per_layer)

    advice: list[str] = []
    if by_mode["shadow"] > 0:
        advice.append("Shadow mode: nothing was withheld. Label a sample with `wakegate review --sample 30` to learn what enforcing would have cost.")
    if suppressed > 0 and sample_n < min_sample:
        advice.append(
            f"Only {sample_n} labelled random samples; at least ~{min_sample} are needed before the miss estimate means much. "
            f"{suppressed} events were suppressed by rules."
        )
    if sample_n > 0 and sample_wanted == 0:
        advice.append(
            f"No wanted events in {sample_n} samples. That bounds the miss rate (see the interval) "
            "but cannot rule out rare misses: a 1% miss rate needs hundreds of labels to see."
        )
    if unlabeled > 0:
        advice.append(f"{unlabeled} sampled events are waiting for a label (wakegate review).")

    by_rule = sorted(
        (
            {"rule": name, "suppressed": b["suppressed"], "sampleSize": b["n"], "wanted": b["wanted"], "ci95": list(wilson(b["wanted"], b["n"]))}
            for name, b in per_rule.items()
        ),
        key=lambda x: (-x["wanted"], -x["suppressed"]),
    )
    outs = store.outcomes()
    delivered_wakes = [r for r in rows if r.action == "wake"]
    counts = {"acted": 0, "noop": 0, "error": 0}
    for r in delivered_wakes:
        o = outs.get(r.id)
        if o:
            counts[o["outcome"]] += 1
    judged = counts["acted"] + counts["noop"]
    outcomes_summary = {
        **counts,
        "wakesDelivered": len(delivered_wakes),
        "wakesWithOutcome": sum(counts.values()),
        # Of the wakes that ended acted or noop, the share that found nothing to do.
        "noopShare": counts["noop"] / judged if judged else None,
    }
    # Shadow mode ground truth: the wakes enforcing would have avoided all happened, so each has an outcome.
    avoidable = [r for r in rows if r.mode == "shadow" and r.verdict != "wake"]
    av = {"noop": 0, "acted": 0, "error": 0}
    for r in avoidable:
        o = outs.get(r.id)
        if o:
            av[o["outcome"]] += 1
    av_judged = av["noop"] + av["acted"]
    avoidance_check = None
    if avoidable:
        lo, hi = wilson(av["acted"], av_judged)
        avoidance_check = {
            "avoidable": len(avoidable), "withOutcome": sum(av.values()), **av,
            # Wakes enforcing would have dropped that turned out to produce something.
            "wouldHaveLostRate": av["acted"] / av_judged if av_judged else None,
            "ci95": [lo, hi] if av_judged else None,
        }

    funnel = []
    for f in store.funnel_rows(since_ms):
        decided = (f["skip"] or 0) + (f["wake"] or 0) + (f["batch"] or 0)
        funnel.append({
            "layer": f["layer"], "kind": f["kind"], "entered": f["entered"],
            "skip": f["skip"] or 0, "wake": f["wake"] or 0, "batch": f["batch"] or 0,
            # Handed on: a defer, or a wake proposal that a governor could still limit.
            "passed": f["entered"] - decided - (f["errors"] or 0) - (f["would_skip"] or 0) if (f["shadow_runs"] or 0) else f["entered"] - decided - (f["errors"] or 0),
            # A layer in shadow mode: what it said, which was not applied.
            "mode": "shadow" if (f["shadow_runs"] or 0) else "enforce",
            "wouldSkip": f["would_skip"] or 0,
            "errors": f["errors"] or 0,
            "costMs": round(f["ms"] or 0, 3), "tokensIn": f["tokens_in"], "tokensOut": f["tokens_out"], "costUsd": f["usd"],
        })
    untraced = store.count_untraced_since(since_ms)

    by_layer = sorted(
        (
            {"layer": name, "suppressed": b["suppressed"], "sampleSize": b["n"], "wanted": b["wanted"], "ci95": list(wilson(b["wanted"], b["n"]))}
            for name, b in per_layer.items()
        ),
        key=lambda x: (-x["wanted"], -x["suppressed"]),
    )
    return {
        "since": iso(since_ms),
        "events": len(rows),
        "funnel": funnel,
        "untracedDecisions": untraced,
        "outcomes": outcomes_summary,
        "avoidanceCheck": avoidance_check,
        "accessLog": store.access_counts_since(since_ms),
        "verdicts": verdicts,
        "byMode": by_mode,
        "topReasons": [
            {"verdict": v, "reason": reason, "n": n}
            for (v, reason), n in sorted(reasons.items(), key=lambda kv: -kv[1])[:8]
        ],
        "wakesAvoidable": shadow_avoidable,
        "estimatedSaving": shadow_avoidable * cost_per_wake if cost_per_wake else None,
        "suppressedByRules": suppressed,
        "labels": labelled,
        "unwantedWakes": unwanted_wakes,
        "unlabeledSamples": unlabeled,
        "missEstimate": miss,
        "byRule": by_rule,
        "byLayer": by_layer,
        "advice": advice,
    }


def review(store: Store, since_ms: int, sample: int | None = None, seed: int | None = None, now: int | None = None,
           layer: str | None = None) -> list[dict[str, Any]]:
    """Draw a uniform random sample of rule-suppressed events for a person to label, and return
    everything still waiting for a label: the new draw, earlier draws and unlabeled canaries."""
    labels = store.labels()
    queued = set(store.queued_ids())
    rows = store.decisions_since(since_ms)

    if sample and sample > 0:
        pool = [
            r for r in rows
            if _is_suppressed(r) and r.mode != "canary" and r.id not in queued and r.id not in labels
            and (layer is None or r.decided_by == layer)
        ]
        rand = seeded_random(seed if seed is not None else now_ms())
        for i in range(len(pool) - 1, 0, -1):
            j = math.floor(rand() * (i + 1))
            pool[i], pool[j] = pool[j], pool[i]
        picked = pool[:sample]
        store.add_to_queue([r.id for r in picked], now if now is not None else now_ms())
        queued.update(r.id for r in picked)
    return [
        brief(r)
        for r in rows
        if r.id not in labels and (r.id in queued or (r.mode == "canary" and _is_suppressed(r)))
    ]
