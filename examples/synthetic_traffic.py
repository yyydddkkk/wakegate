"""Synthetic traffic with ground-truth labels, shared by the examples. All of it is the author's guess."""

from __future__ import annotations

import math
from dataclasses import dataclass

from wakegate import WakeEvent
from wakegate.hashing import seeded_random

DAY = 86_400_000
HOUR = 3_600_000
MIN = 60_000
DAYS = 14
T0 = 1_767_571_200_000  # Date.UTC(2026, 0, 5)


@dataclass
class Labeled:
    event: WakeEvent
    label: str  # "must" | "nice" | "redundant" | "noise"


def _round(x: float) -> int:
    return math.floor(x + 0.5)  # JavaScript's Math.round


def generate(seed: int) -> list[Labeled]:
    r = seeded_random(seed)

    def poisson(lam: float) -> int:
        limit, k, p = math.exp(-lam), 0, 1.0
        while True:
            k += 1
            p *= r()
            if not p > limit:
                break
        return k - 1

    def work_hour() -> float:
        return 9 * HOUR + r() * 9 * HOUR  # 09:00-18:00 UTC

    def any_hour() -> float:
        return r() * DAY

    out: list[Labeled] = []

    def push(ts: float, source: str, key: str, data: dict, label: str, severity: str | None = None) -> None:
        out.append(Labeled(WakeEvent(source, key, _round(ts), severity or None, data), label))

    pr = thread = inc = 0
    for d in range(DAYS):
        day = T0 + d * DAY

        for i in range(48):
            push(day + i * 30 * MIN, "heartbeat", "main", {"status": "HEARTBEAT_OK", "checkedAt": day + i * 30 * MIN}, "noise")

        # GitHub: human PRs with comment bursts, bot PRs, and some deploy-bot failures that matter.
        for _ in range(poisson(8)):
            late = r() < 0.1  # some threads happen in the evening, 21:00-24:00 UTC
            t = day + (21 * HOUR + r() * 3 * HOUR if late else work_hour())
            pr += 1
            key = f"pr/{pr}"
            if r() < 0.7:
                wants_review = r() < 0.4
                push(t, "github", key, {"state": "review_requested" if wants_review else "opened", "actor": "alice"}, "must" if wants_review else "nice")
                # Opened first, review requested a few minutes later on the same PR: the second one matters.
                if not wants_review and r() < 0.3:
                    push(t + (2 + r() * 18) * MIN, "github", key, {"state": "review_requested", "actor": "alice"}, "must")
                tc = t
                n = 1 + math.floor(r() * 5)
                for c in range(n):
                    tc += (1 + r() * 7) * MIN
                    push(tc, "github", key, {"state": "commented", "n": c, "actor": "alice"}, "nice" if c == 0 else "redundant")
            elif r() < 0.08:
                push(t, "github", key, {"state": "deploy_failed", "actor": "deploy-bot[bot]"}, "must")
            else:
                push(t, "github", key, {"state": "opened", "actor": "dependabot[bot]"}, "noise")

        # Mail: mostly newsletters; some human; a few from a VIP that need action.
        for _ in range(poisson(12)):
            t = day + (work_hour() if r() < 0.8 else any_hour())
            thread += 1
            key = f"thread/{thread}"
            x = r()
            if x < 0.65:
                push(t, "mail", key, {"kind": "newsletter", "subject": "Weekly digest"}, "noise")
            elif x < 0.95:
                push(t, "mail", key, {"kind": "human", "subject": "Quick question"}, "nice")
            else:
                push(t, "mail", key, {"kind": "human", "vip": True, "subject": "Need your call today"}, "must")

        # Pager: critical at any hour; identical re-alerts until acked; sometimes escalates.
        for _ in range(poisson(0.6)):
            t = day + any_hour()
            inc += 1
            key = f"inc/{inc}"
            push(t, "pagerduty", key, {"state": "triggered"}, "must", "critical")
            repeats = 1 + math.floor(r() * 3)
            for k in range(1, repeats + 1):
                push(t + k * 15 * MIN, "pagerduty", key, {"state": "triggered"}, "redundant", "critical")
            if r() < 0.3:
                push(t + (repeats + 1) * 15 * MIN, "pagerduty", key, {"state": "escalated"}, "must", "critical")

        # CI: runs are always "new" data; success is noise, main failures matter.
        for run in range(poisson(10)):
            t = day + work_hour()
            on_main = r() < 0.4
            fail = r() < (0.06 if on_main else 0.2)
            key = "main" if on_main else f"branch/{math.floor(r() * 5)}"
            label = ("must" if on_main else "nice") if fail else "noise"
            push(t, "ci", key, {"result": "failure" if fail else "success", "branch": "main" if on_main else "feature", "run": d * 100 + run}, label)

    out.sort(key=lambda e: e.event.ts)
    return out
