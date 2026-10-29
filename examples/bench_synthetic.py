"""Synthetic stress test: does the gate lose events that matter?

Every event carries a ground-truth label written by the generator, so misses can be counted.
That is also the weakness: the traffic mix, the labels and the traps are the author's guesses.
The result shows WHICH mechanisms can lose events and by roughly how much under these patterns.
It does not estimate real-world rates.

    uv run python examples/bench_synthetic.py [seeds=5]
"""

from __future__ import annotations

import sys
from pathlib import Path

from synthetic_traffic import DAY, DAYS, HOUR, MIN, T0, generate

from wakegate import Gate, Store, parse_config

NAIVE = "dedupe: false"

# BEFORE: the example config as it shipped in the first commits, and the old semantics
# (no exempt, batches only delivered by an explicit flush).
SHIPPED_BEFORE = r"""
flushOnWake: false
ignorePaths: [checkedAt]
bypass: [{ name: critical, when: { path: severity, eq: critical } }]
rules: [{ name: ignore-bots, when: { path: data.actor, matches: "\\[bot\\]$" }, then: skip }]
cooldownSeconds: 1800
budget: { maxWakesPerDay: 10, overBudget: batch, quietHours: { start: "22:00", end: "07:00" }, timezone: UTC }
"""
# AFTER: the example config as it ships now.
SHIPPED_AFTER = (Path(__file__).parent / "wakegate.yaml").read_text()


def tuned(extra: str, after: bool) -> str:
    """A config tuned to this traffic. `after` adds exempt for review requests and flushOnWake."""
    review = "  - { name: review-requests, when: { path: data.state, eq: review_requested }, then: wake, exempt: [cooldown, budget] }\n" if after else ""
    return (
        f"\nflushOnWake: {'true' if after else 'false'}\nignorePaths: [checkedAt]\nbypass:\n"
        "  - { name: critical, when: { path: severity, eq: critical } }\n"
        "  - { name: deploy-failed, when: { path: data.state, eq: deploy_failed } }\n"
        "  - { name: vip-mail, when: { path: data.vip, eq: true } }\n"
        "  - { name: main-ci-failure, when: { all: [{ path: key, eq: main }, { path: data.result, eq: failure }] } }\n"
        f"rules:\n{review}"
        "  - { name: newsletters, when: { path: data.kind, eq: newsletter }, then: skip }\n"
        "  - { name: ci-success, when: { path: data.result, eq: success }, then: skip }\n"
        "  - { name: heartbeat, when: { path: source, eq: heartbeat }, then: skip }\n"
        '  - { name: ignore-bots, when: { path: data.actor, matches: "\\\\[bot\\\\]$" }, then: skip }\n'
        f"{extra}\n"
    )


QUIET = "quietHours: { start: '22:00', end: '07:00' }, timezone: UTC"


def full(after: bool, over: str) -> str:
    return tuned(f"cooldownSeconds: 1800\nbudget: {{ maxWakesPerDay: 10, {over}{QUIET} }}", after)


CONFIGS = [
    ("naive: wake on everything", NAIVE),
    ("BEFORE shipped example", SHIPPED_BEFORE),
    ("AFTER  shipped example", SHIPPED_AFTER),
    ("BEFORE tuned, cooldown only", tuned("cooldownSeconds: 1800", False)),
    ("AFTER  tuned, cooldown only", tuned("cooldownSeconds: 1800", True)),
    ("BEFORE tuned, full (batch)", full(False, "")),
    ("AFTER  tuned, full (batch)", full(True, "")),
    ("BEFORE tuned, full (skip)", full(False, "overBudget: skip, ")),
    ("AFTER  tuned, full (skip)", full(True, "overBudget: skip, ")),
]


def run(events, yaml: str, flush: str) -> dict:
    store = Store(":memory:")
    gate = Gate(parse_config(yaml), store)
    label_by_id, ts_by_id, delivered, kind, reason_by_id = {}, {}, {}, {}, {}
    wakes = 0
    last_flush_day = -1

    for item in events:
        event, label = item.event, item.label
        ts = event.ts
        if flush != "never":
            day = (ts - T0) // DAY
            if day > last_flush_day and ts - T0 - day * DAY >= 8 * HOUR:
                at = T0 + day * DAY + 8 * HOUR
                for row in store.take_pending():
                    delivered[row.id] = at
                last_flush_day = day
        d = gate.decide(event)
        label_by_id[d.id] = label
        ts_by_id[d.id] = ts
        reason_by_id[d.id] = f"{d.action}:{d.reason}"
        if d.action == "wake":
            wakes += 1
            delivered[d.id] = ts
            kind[d.id] = "wake"
            for b in d.batch or []:
                delivered[b["id"]] = ts  # flushOnWake: batch rides along
        elif d.action == "skip":
            kind[d.id] = "skip"
    store.close()

    must = [i for i, l in label_by_id.items() if l == "must"]
    nice = [i for i, l in label_by_id.items() if l == "nice"]
    within = lambda ids, ms: sum(1 for i in ids if i in delivered and delivered[i] - ts_by_id[i] <= ms)  # noqa: E731
    pct = lambda n, d: (100 * n / d) if d else 100  # noqa: E731
    wasted = sum(1 for i, l in label_by_id.items() if kind.get(i) == "wake" and l in ("noise", "redundant"))
    delays = sorted(delivered[i] - ts_by_id[i] for i in must if i in delivered)
    lost: dict[str, int] = {}
    late: dict[str, int] = {}
    for i in must:
        t = delivered.get(i)
        if t is None:
            lost[reason_by_id[i]] = lost.get(reason_by_id[i], 0) + 1
        elif t - ts_by_id[i] > 0:
            late[reason_by_id[i]] = late.get(reason_by_id[i], 0) + 1
    return {
        "lost": lost, "late": late, "wakes": wakes,
        "must_now": pct(within(must, 0), len(must)),
        "must_1h": pct(within(must, HOUR), len(must)),
        "must_ever": pct(sum(1 for i in must if i in delivered), len(must)),
        "nice_ever": pct(sum(1 for i in nice if i in delivered), len(nice)),
        "wasted": wasted,
        "p95_delay_min": delays[int(len(delays) * 0.95)] / MIN if delays else 0,
    }


def main() -> None:
    seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    datasets = [generate(1000 + i) for i in range(seeds)]
    avg = lambda xs: sum(xs) / len(xs)  # noqa: E731
    f = lambda n, w=5, d=1: f"{n:.{d}f}".rjust(w)  # noqa: E731

    print(f"{DAYS} days x {seeds} seeds. Avg {round(avg([len(d) for d in datasets]))} events and "
          f"{round(avg([sum(1 for e in d if e.label == 'must') for d in datasets]))} must-wake events per run.")
    print("Columns: wakes/run | must-wake delivered: instantly / within 1h / ever (worst seed) | nice-to-know ever | "
          "wasted wakes (noise+redundant) | p95 delay of must-wake delivered (min)\n")

    breakdown = []
    for flush in ("never", "daily-08:00"):
        print("--- batch delivery: " + ("the agent never calls flush (only flushOnWake can deliver a batch)" if flush == "never"
                                         else "the agent also flushes by hand daily at 08:00 UTC") + " ---")
        for name, yaml in CONFIGS:
            rs = [run(d, yaml, flush) for d in datasets]
            if flush == "never":
                breakdown.append((name, rs))
            worst = min(x["must_ever"] for x in rs)
            print(f"{name.ljust(28)} {f(avg([x['wakes'] for x in rs]), 6, 0)} | "
                  f"{f(avg([x['must_now'] for x in rs]))}% {f(avg([x['must_1h'] for x in rs]))}% {f(avg([x['must_ever'] for x in rs]))}% ({f(worst)}%) | "
                  f"{f(avg([x['nice_ever'] for x in rs]))}% | {f(avg([x['wasted'] for x in rs]), 5, 0)} | {f(avg([x['p95_delay_min'] for x in rs]), 6, 0)}")
        print()

    print("--- why must-wake events did not wake instantly (agent never flushes by hand; summed over seeds) ---")

    def total(rs, k):
        t: dict[str, int] = {}
        for x in rs:
            for a, n in x[k].items():
                t[a] = t.get(a, 0) + n
        return ", ".join(f"{a} x{n}" for a, n in sorted(t.items(), key=lambda kv: -kv[1])) or "none"

    for name, rs in breakdown:
        if name.startswith("naive"):
            continue
        print(f"{name.strip()}\n    never delivered: {total(rs, 'lost')}\n    delivered late:  {total(rs, 'late')}")


if __name__ == "__main__":
    main()
