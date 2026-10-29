"""Does the canary estimate recover the true miss rate?

For each config we know the truth (the generator's labels): among events the rules suppressed
(skips other than duplicates), what share were must-wake events? We run the gate with a canary,
label every canary from the ground truth as a perfect reviewer would, and compare wakegate's
estimate and its 95% interval with the truth.

It tests the estimator, not the rules, and assumes labels are always right. Real reviewers are
slower and less certain. The traffic is the author's guess.

    uv run python examples/bench_canary.py [seeds=40]
"""

from __future__ import annotations

import sys

from synthetic_traffic import MIN, T0, generate

from wakegate import Gate, Store, parse_config
from wakegate.queries import report

BASE = r"""
ignorePaths: [checkedAt]
bypass:
  - { name: critical, when: { path: severity, eq: critical } }
  - { name: vip-mail, when: { path: data.vip, eq: true } }
  - { name: main-ci-failure, when: { all: [{ path: key, eq: main }, { path: data.result, eq: failure }] } }
rules:
  - { name: newsletters, when: { path: data.kind, eq: newsletter }, then: skip }
  - { name: ci-success, when: { path: data.result, eq: success }, then: skip }
  - { name: heartbeat, when: { path: source, eq: heartbeat }, then: skip }
  - { name: ignore-bots, when: { path: data.actor, matches: "\\[bot\\]$" }, then: skip }
"""
MAIN_FAIL = "  - { name: main-ci-failure, when: { all: [{ path: key, eq: main }, { path: data.result, eq: failure }] } }\n"
# A: the bot rule swallows deploy-bot failures (a few real misses).
# B: drops everything from CI and has no bypass for failures on main (many real misses).
CONFIGS = [
    ("A: bot rule hides deploy failures", BASE),
    ("B: all CI skipped, no bypass for main failures",
     BASE.replace(MAIN_FAIL, "").replace("rules:\n", "rules:\n  - { name: no-ci, when: { path: source, eq: ci }, then: skip }\n")),
]


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    seeds = int(argv[0]) if argv else 40
    print(f"{seeds} seeds, 14 days of synthetic traffic each. A perfect reviewer labels every canary.\n")
    pct = lambda x: f"{100 * x:.1f}%".rjust(6)  # noqa: E731
    for name, yaml in CONFIGS:
        print(name)
        for rate in (0.02, 0.05, 0.2):
            covered = usable = 0
            est_sum = truth_sum = width_sum = n_sum = sup_sum = 0.0
            for s in range(seeds):
                store = Store(":memory:")
                gate = Gate(parse_config(f"canary: {{ rate: {rate} }}\n{yaml}"), store)
                suppressed = must_suppressed = 0
                for item in generate(5000 + s):
                    d = gate.decide(item.event)
                    if d.verdict == "skip" and d.reason != "duplicate":
                        suppressed += 1
                        must_suppressed += item.label == "must"
                    if d.canary:
                        store.set_label(d.id, "wanted" if item.label == "must" else "unwanted", None, T0 + MIN)
                r = report(store, 0, min_sample=1)
                store.close()
                est = r["missEstimate"]
                if not est or suppressed == 0:
                    continue
                truth = must_suppressed / suppressed
                usable += 1
                est_sum += est["rate"]
                truth_sum += truth
                width_sum += est["ci95"][1] - est["ci95"][0]
                n_sum += est["sampleSize"]
                sup_sum += suppressed
                covered += est["ci95"][0] <= truth <= est["ci95"][1]
            print(f"  canary {pct(rate)}: ~{n_sum / usable:3.0f} labels of {sup_sum / usable:.0f} suppressed | true miss rate {pct(truth_sum / usable)} "
                  f"| estimate {pct(est_sum / usable)} | mean CI width {pct(width_sum / usable)} | CI contained the truth in {covered}/{usable} runs")
        print()


if __name__ == "__main__":
    main()
