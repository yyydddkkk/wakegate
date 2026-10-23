// Does the canary estimate recover the true miss rate?
//
// For each config we know the truth (the generator's labels): among events the rules
// suppressed (skips other than duplicates), what share were must-wake events? We run the
// gate with a canary, label every canary from the ground truth as a perfect reviewer
// would, and compare wakegate's estimate and its 95% interval with the truth.
//
// It tests the estimator, not the rules, and assumes labels are always right. Real
// reviewers are slower and less certain. The traffic is the author's guess.
//
//   node examples/bench-canary.ts [seeds=40]

import { parseConfig } from "../src/config.ts";
import { Gate } from "../src/gate.ts";
import { report } from "../src/queries.ts";
import { Store } from "../src/store.ts";
import { generate, T0, MIN } from "./synthetic-traffic.ts";

const BASE = `
ignorePaths: [checkedAt]
bypass:
  - { name: critical, when: { path: severity, eq: critical } }
  - { name: vip-mail, when: { path: data.vip, eq: true } }
  - { name: main-ci-failure, when: { all: [{ path: key, eq: main }, { path: data.result, eq: failure }] } }
rules:
  - { name: newsletters, when: { path: data.kind, eq: newsletter }, then: skip }
  - { name: ci-success, when: { path: data.result, eq: success }, then: skip }
  - { name: heartbeat, when: { path: source, eq: heartbeat }, then: skip }
  - { name: ignore-bots, when: { path: data.actor, matches: "\\\\[bot\\\\]$" }, then: skip }
`;
// A: the bot rule swallows deploy-bot failures (a few real misses).
// B: drops everything from CI and has no bypass for failures on main (many real misses).
const CONFIGS: Array<[string, string]> = [
  ["A: bot rule hides deploy failures", BASE],
  ["B: all CI skipped, no bypass for main failures", BASE.replace("  - { name: main-ci-failure, when: { all: [{ path: key, eq: main }, { path: data.result, eq: failure }] } }\n", "").replace("rules:\n", "rules:\n  - { name: no-ci, when: { path: source, eq: ci }, then: skip }\n")],
];

const seeds = Number(process.argv[2] ?? 40);
const rates = [0.02, 0.05, 0.2];
const wilsonWidth = (ci: [number, number]) => ci[1] - ci[0];

console.log(`${seeds} seeds, 14 days of synthetic traffic each. A perfect reviewer labels every canary.\n`);
for (const [name, yaml] of CONFIGS) {
  console.log(name);
  for (const rate of rates) {
    let covered = 0, usable = 0, estSum = 0, truthSum = 0, widthSum = 0, nSum = 0, supSum = 0;
    for (let s = 0; s < seeds; s++) {
      const events = generate(5000 + s);
      const store = new Store(":memory:");
      const gate = new Gate(parseConfig(`canary: { rate: ${rate} }\n${yaml}`), store);
      let suppressed = 0, mustSuppressed = 0;
      for (const { event, label } of events) {
        const d = gate.decide(event);
        const isSuppressed = d.verdict === "skip" && d.reason !== "duplicate";
        if (isSuppressed) {
          suppressed++;
          if (label === "must") mustSuppressed++;
        }
        if (d.canary) store.setLabel(d.id, label === "must" ? "wanted" : "unwanted", null, T0 + MIN);
      }
      const r = report(store, 0, { minSample: 1 });
      store.close();
      if (!r.missEstimate || suppressed === 0) continue;
      const truth = mustSuppressed / suppressed;
      usable++;
      estSum += r.missEstimate.rate;
      truthSum += truth;
      widthSum += wilsonWidth(r.missEstimate.ci95);
      nSum += r.missEstimate.sampleSize;
      supSum += suppressed;
      if (truth >= r.missEstimate.ci95[0] && truth <= r.missEstimate.ci95[1]) covered++;
    }
    const pct = (x: number) => `${(100 * x).toFixed(1)}%`.padStart(6);
    console.log(
      `  canary ${pct(rate)}: ~${(nSum / usable).toFixed(0).padStart(3)} labels of ${(supSum / usable).toFixed(0)} suppressed | true miss rate ${pct(truthSum / usable)} | estimate ${pct(estSum / usable)} | mean CI width ${pct(widthSum / usable)} | CI contained the truth in ${covered}/${usable} runs`,
    );
  }
  console.log();
}
