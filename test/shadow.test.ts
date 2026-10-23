import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { DatabaseSync } from "node:sqlite";
import { parseConfig } from "../src/config.ts";
import { Gate } from "../src/gate.ts";
import { catchup, report, review, wilson } from "../src/queries.ts";
import { replay } from "../src/replay.ts";
import { Store } from "../src/store.ts";

const T0 = Date.UTC(2026, 0, 5, 12, 0, 0);
const MIN = 60_000;

const SKIP_NOISE = `
rules: [{ name: noise, when: { path: data.kind, eq: noise }, then: skip }]
`;

function setup(yaml: string) {
  const store = new Store(":memory:");
  return { store, gate: new Gate(parseConfig(yaml), store) };
}
const ev = (i: number, data: Record<string, unknown> = { kind: "noise" }) => ({ source: "s", key: `k${i}`, ts: T0 + i * MIN, data: { ...data, i } });

test("shadow: everything wakes, the verdict is recorded, nothing is batched", () => {
  const { gate, store } = setup(`mode: shadow\n${SKIP_NOISE}cooldownSeconds: 600`);
  const noise = gate.decide(ev(1));
  assert.equal(noise.action, "wake");
  assert.equal(noise.verdict, "skip");
  assert.equal(noise.mode, "shadow");
  assert.equal(noise.reason, "rule:noise");

  gate.decide({ source: "s", key: "x", ts: T0, data: { kind: "real", n: 1 } });
  const cooled = gate.decide({ source: "s", key: "x", ts: T0 + MIN, data: { kind: "real", n: 2 } });
  assert.equal(cooled.action, "wake");
  assert.equal(cooled.verdict, "batch"); // cooldown is simulated from verdicts
  assert.equal(store.pendingCount(), 0, "nothing is held back in shadow mode");
});

test("shadow simulates the budget from verdicts, so it matches enforcement", () => {
  const cfg = "budget: { maxWakesPerDay: 2, timezone: UTC }";
  const enforce = setup(cfg), shadow = setup(`mode: shadow\n${cfg}`);
  const run = (g: Gate) => [1, 2, 3, 4].map((i) => g.decide(ev(i, { kind: "real" })).verdict);
  assert.deepEqual(run(shadow.gate), run(enforce.gate));
  assert.deepEqual(run(new Gate(parseConfig(cfg), new Store(":memory:"))), ["wake", "wake", "batch", "batch"]);
});

test("config rejects a canary in shadow mode and a bad rate", () => {
  assert.throws(() => parseConfig("mode: shadow\ncanary: { rate: 0.1 }"), /no effect in shadow mode/);
  assert.throws(() => parseConfig("canary: { rate: 2 }"), /between 0 and 1/);
  assert.throws(() => parseConfig("mode: dry"), /enforce.*shadow/);
});

test("canary releases a would-be skip as a wake, names the id, and never releases duplicates", () => {
  const { gate } = setup(`canary: { rate: 1 }\n${SKIP_NOISE}`);
  const d = gate.decide(ev(1));
  assert.equal(d.action, "wake");
  assert.equal(d.verdict, "skip");
  assert.equal(d.mode, "canary");
  assert.equal(d.canary, true);
  assert.match(d.note!, new RegExp(`label ${d.id} wanted\\|unwanted`));

  const dup = gate.decide(ev(1));
  assert.equal(dup.reason, "duplicate");
  assert.equal(dup.action, "skip", "duplicates carry no new information and are never canaried");
});

test("canary rate 0 releases nothing; rate 0.25 releases roughly a quarter, reproducibly", () => {
  const releases = (yaml: string) => {
    const { gate } = setup(yaml);
    let n = 0;
    for (let i = 0; i < 2000; i++) if (gate.decide(ev(i)).mode === "canary") n++;
    return n;
  };
  assert.equal(releases(SKIP_NOISE), 0);
  const a = releases(`canary: { rate: 0.25 }\n${SKIP_NOISE}`);
  assert.ok(a > 400 && a < 600, `expected about 500, got ${a}`);
  assert.equal(releases(`canary: { rate: 0.25 }\n${SKIP_NOISE}`), a, "same input, same canaries");
});

test("a canary does not spend the daily budget", () => {
  const { gate } = setup(`canary: { rate: 1 }\nbudget: { maxWakesPerDay: 1, timezone: UTC }\n${SKIP_NOISE}`);
  gate.decide(ev(1)); // canary
  gate.decide(ev(2)); // canary
  assert.equal(gate.decide(ev(3, { kind: "real" })).action, "wake", "the one real wake is still available");
});

test("labels from canaries and from the review queue count as samples; others do not", () => {
  const { gate, store } = setup(`canary: { rate: 0 }\n${SKIP_NOISE}`);
  const a = gate.decide(ev(1)); // plain skip
  assert.equal(store.setLabel(a.id, "wanted", null, T0).sample, false);
  const b = gate.decide(ev(2));
  store.addToQueue([b.id], T0);
  assert.equal(store.setLabel(b.id, "wanted", null, T0).sample, true);
  assert.throws(() => store.setLabel(9999, "wanted", null, T0), /no decision/);

  const c = setup(`canary: { rate: 1 }\n${SKIP_NOISE}`);
  const d = c.gate.decide(ev(3));
  assert.equal(c.store.setLabel(d.id, "unwanted", null, T0).sample, true);
});

test("wilson interval matches known values", () => {
  const [lo0, hi0] = wilson(0, 10);
  assert.ok(lo0 === 0 && Math.abs(hi0 - 0.2775) < 0.001);
  const [lo, hi] = wilson(5, 10);
  assert.ok(Math.abs(lo - 0.2366) < 0.001 && Math.abs(hi - 0.7634) < 0.001);
  assert.deepEqual(wilson(0, 0), [0, 1]);
});

test("report turns labelled canaries into a miss estimate with an interval", () => {
  const { gate, store } = setup(`canary: { rate: 1 }\n${SKIP_NOISE}`);
  const ids = Array.from({ length: 40 }, (_, i) => gate.decide(ev(i)).id);
  assert.equal(report(store, T0 - MIN).missEstimate, null);
  assert.equal(report(store, T0 - MIN).unlabeledSamples, 40);

  ids.forEach((id, i) => store.setLabel(id, i < 10 ? "wanted" : "unwanted", null, T0));
  const r = report(store, T0 - MIN);
  assert.equal(r.missEstimate!.sampleSize, 40);
  assert.equal(r.missEstimate!.rate, 0.25);
  assert.ok(r.missEstimate!.ci95[0] < 0.25 && r.missEstimate!.ci95[1] > 0.25);
  assert.equal(r.suppressedByRules, 40);
  assert.equal(r.unlabeledSamples, 0);
  assert.equal(r.advice.some((a) => /labelled random samples/.test(a)), false, "40 labels is enough to stop warning");
});

test("report breaks the estimate down by the rule that suppressed the event", () => {
  const { gate, store } = setup(`canary: { rate: 1 }
rules:
  - { name: noise, when: { path: data.kind, eq: noise }, then: skip }
  - { name: too-broad, when: { path: data.kind, eq: alert }, then: skip }
`);
  for (let i = 0; i < 10; i++) {
    const a = gate.decide(ev(i, { kind: "noise" }));
    store.setLabel(a.id, "unwanted", null, T0);
    const b = gate.decide(ev(100 + i, { kind: "alert" }));
    store.setLabel(b.id, i < 6 ? "wanted" : "unwanted", null, T0);
  }
  const r = report(store, T0 - 200 * MIN);
  const by = Object.fromEntries(r.byRule.map((x) => [x.rule, x]));
  assert.equal(by["too-broad"]!.wanted, 6);
  assert.equal(by["too-broad"]!.sampleSize, 10);
  assert.equal(by["noise"]!.wanted, 0);
  assert.equal(r.byRule[0]!.rule, "too-broad", "the rule hiding wanted events comes first");
});

test("report warns when the sample is small and ignores non-random labels", () => {
  const { gate, store } = setup(SKIP_NOISE); // canary off
  const d = gate.decide(ev(1));
  store.setLabel(d.id, "wanted", null, T0); // manual label, not a sample
  const r = report(store, T0 - MIN);
  assert.equal(r.missEstimate, null);
  assert.equal(r.labels.wanted, 1);
  assert.match(r.advice.join(" "), /labelled random samples/);
});

test("shadow report counts avoidable wakes and prices them", () => {
  const { gate, store } = setup(`mode: shadow\n${SKIP_NOISE}`);
  for (let i = 0; i < 5; i++) gate.decide(ev(i));
  gate.decide({ source: "s", key: "real", ts: T0, data: { kind: "real" } });
  const r = report(store, T0 - MIN, { costPerWake: 2 });
  assert.equal(r.byMode.shadow, 6);
  assert.equal(r.wakesAvoidable, 5);
  assert.equal(r.estimatedSaving, 10);
});

test("review draws a seeded random sample of rule skips, never duplicates, canaries or labelled ones", () => {
  const { gate, store } = setup(SKIP_NOISE);
  const ids = Array.from({ length: 30 }, (_, i) => gate.decide(ev(i)).id);
  gate.decide(ev(0)); // a duplicate
  store.setLabel(ids[0]!, "unwanted", null, T0);

  const first = review(store, T0 - MIN, { sample: 5, seed: 7 });
  assert.equal(first.length, 5);
  assert.ok(first.every((b) => b.reason === "rule:noise" && b.id !== ids[0]));
  // Asking again without a new draw returns the same waiting items.
  assert.deepEqual(review(store, T0 - MIN).map((b) => b.id), first.map((b) => b.id));
  // Labelling removes an item from the waiting list.
  store.setLabel(first[0]!.id, "unwanted", null, T0);
  assert.equal(review(store, T0 - MIN).length, 4);
  // Same seed on a fresh store picks the same events.
  const again = setup(SKIP_NOISE);
  for (let i = 0; i < 30; i++) again.gate.decide(ev(i));
  again.gate.decide(ev(0));
  again.store.setLabel(ids[0]!, "unwanted", null, T0);
  assert.deepEqual(review(again.store, T0 - MIN, { sample: 5, seed: 7 }).map((b) => b.id), first.map((b) => b.id));
});

test("catchup follows the verdict, so shadow mode still hides rule-skipped noise", () => {
  const { gate, store } = setup(`mode: shadow\n${SKIP_NOISE}`);
  gate.decide(ev(1));
  gate.decide({ source: "s", key: "real", ts: T0, data: { kind: "real" } });
  const c = catchup(store, T0 - MIN);
  assert.deepEqual(c.changed.map((x) => x.key), ["real"]);
  assert.equal(c.filteredBySkipRules, 1);
});

test("replay counts verdicts and charges canaries as wakes", () => {
  const events = Array.from({ length: 200 }, (_, i) => ev(i));
  const none = replay(events, parseConfig(SKIP_NOISE), 1);
  assert.equal(none.counts.skip, 200);
  assert.equal(none.canariesReleased, 0);
  const some = replay(events, parseConfig(`canary: { rate: 0.5 }\n${SKIP_NOISE}`), 1);
  assert.ok(some.canariesReleased > 60 && some.canariesReleased < 140);
  assert.equal(some.gatedCost, some.canariesReleased);
  const shadow = replay(events, parseConfig(`mode: shadow\n${SKIP_NOISE}`), 1);
  assert.equal(shadow.counts.skip, 200, "shadow replay shows what enforcing would do");
});

test("a database from before shadow/canary is upgraded in place", () => {
  const path = join(mkdtempSync(join(tmpdir(), "wakegate-")), "old.db");
  const old = new DatabaseSync(path);
  old.exec(`
    CREATE TABLE decisions (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, day TEXT NOT NULL,
      source TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
      action TEXT NOT NULL, reason TEXT NOT NULL, rule TEXT, changed INTEGER NOT NULL, event_json TEXT NOT NULL
    );
    INSERT INTO decisions (ts, day, source, key, fingerprint, action, reason, rule, changed, event_json)
    VALUES (1, '2026-01-05', 's', 'k', 'f', 'skip', 'rule:x', 'x', 1, '{"source":"s","key":"k"}');
  `);
  old.close();

  const store = new Store(path);
  const row = store.getDecision(1)!;
  assert.equal(row.verdict, "skip", "verdict is backfilled from action");
  assert.equal(row.mode, "enforce");
  store.close();
});
