import assert from "node:assert/strict";
import { test } from "node:test";
import { parseConfig } from "../src/config.ts";
import { Gate, inQuietHours } from "../src/gate.ts";
import { replay } from "../src/replay.ts";
import { Store } from "../src/store.ts";

const T0 = Date.UTC(2026, 0, 5, 12, 0, 0); // Mon 12:00 UTC
const MIN = 60_000;

function setup(yaml: string) {
  const store = new Store(":memory:");
  return { store, gate: new Gate(parseConfig(yaml), store) };
}

test("identical payload for the same key is a duplicate", () => {
  const { gate } = setup("version: 1");
  assert.equal(gate.decide({ source: "s", key: "k", ts: T0, data: { n: 1 } }).action, "wake");
  const d = gate.decide({ source: "s", key: "k", ts: T0 + MIN, data: { n: 1 } });
  assert.equal(d.action, "skip");
  assert.equal(d.reason, "duplicate");
  assert.equal(gate.decide({ source: "s", key: "k", ts: T0 + 2 * MIN, data: { n: 2 } }).action, "wake");
});

test("ignorePaths stops noisy fields from counting as a change", () => {
  const { gate } = setup("ignorePaths: [fetchedAt, meta.id]");
  gate.decide({ source: "s", key: "k", ts: T0, data: { v: 1, fetchedAt: 1, meta: { id: "a" } } });
  const d = gate.decide({ source: "s", key: "k", ts: T0 + MIN, data: { v: 1, fetchedAt: 2, meta: { id: "b" } } });
  assert.equal(d.reason, "duplicate");
});

test("different keys and sources are tracked independently", () => {
  const { gate } = setup("version: 1");
  gate.decide({ source: "a", key: "k", ts: T0, data: 1 });
  assert.equal(gate.decide({ source: "b", key: "k", ts: T0, data: 1 }).action, "wake");
  assert.equal(gate.decide({ source: "a", key: "other", ts: T0, data: 1 }).action, "wake");
});

test("first matching rule decides; default applies otherwise", () => {
  const { gate } = setup(`
rules:
  - { name: bots, when: { path: data.actor, matches: "\\\\[bot\\\\]$" }, then: skip }
  - { name: docs, when: { path: data.kind, eq: docs }, then: batch }
default: wake
`);
  assert.equal(gate.decide({ source: "s", key: "1", ts: T0, data: { actor: "ci[bot]" } }).reason, "rule:bots");
  assert.equal(gate.decide({ source: "s", key: "2", ts: T0, data: { kind: "docs" } }).action, "batch");
  assert.equal(gate.decide({ source: "s", key: "3", ts: T0, data: { kind: "bug" } }).reason, "default");
});

test("bypass wakes through quiet hours, cooldown and an exhausted budget", () => {
  const { gate } = setup(`
bypass:
  - { name: critical, when: { path: severity, eq: critical } }
cooldownSeconds: 3600
budget: { maxWakesPerDay: 0, quietHours: { start: "00:00", end: "23:59" }, timezone: UTC }
`);
  assert.equal(gate.decide({ source: "s", key: "a", ts: T0, data: 1 }).action, "batch");
  const d = gate.decide({ source: "s", key: "a", ts: T0 + MIN, data: 2, severity: "critical" });
  assert.equal(d.action, "wake");
  assert.equal(d.reason, "bypass:critical");
});

test("cooldown batches changes shortly after a wake, then allows again", () => {
  const { gate } = setup("cooldownSeconds: 600");
  assert.equal(gate.decide({ source: "s", key: "k", ts: T0, data: 1 }).action, "wake");
  assert.equal(gate.decide({ source: "s", key: "k", ts: T0 + 5 * MIN, data: 2 }).reason, "cooldown");
  assert.equal(gate.decide({ source: "s", key: "k", ts: T0 + 11 * MIN, data: 3 }).action, "wake");
});

test("daily budget caps wakes, resets next day, and overBudget can skip", () => {
  const { gate } = setup("budget: { maxWakesPerDay: 2, timezone: UTC, overBudget: skip }");
  const wake = (key: string, ts: number) => gate.decide({ source: "s", key, ts, data: key }).action;
  assert.equal(wake("a", T0), "wake");
  assert.equal(wake("b", T0 + MIN), "wake");
  assert.equal(wake("c", T0 + 2 * MIN), "skip");
  assert.equal(wake("d", T0 + 24 * 60 * MIN), "wake");
});

test("quiet hours wrap midnight and use the configured timezone", () => {
  assert.equal(inQuietHours(Date.UTC(2026, 0, 5, 23, 0), "22:00", "07:00", "UTC"), true);
  assert.equal(inQuietHours(Date.UTC(2026, 0, 5, 3, 0), "22:00", "07:00", "UTC"), true);
  assert.equal(inQuietHours(Date.UTC(2026, 0, 5, 12, 0), "22:00", "07:00", "UTC"), false);
  // 23:00 UTC is 07:00 next day in Shanghai: just outside the window.
  assert.equal(inQuietHours(Date.UTC(2026, 0, 5, 23, 0), "22:00", "07:00", "Asia/Shanghai"), false);
});

test("batched events are returned once by takePending, then cleared", () => {
  const { gate, store } = setup("rules: [{ name: later, when: { path: source, eq: s }, then: batch }]");
  gate.decide({ source: "s", key: "1", ts: T0, data: 1 });
  gate.decide({ source: "s", key: "2", ts: T0, data: 2 });
  assert.equal(store.takePending().length, 2);
  assert.equal(store.takePending().length, 0);
});

test("every decision, including skips, lands in the audit log with a reason", () => {
  const { gate, store } = setup("version: 1");
  gate.decide({ source: "s", key: "k", ts: T0, data: 1 });
  gate.decide({ source: "s", key: "k", ts: T0 + MIN, data: 1 });
  const log = store.recentDecisions(10);
  assert.deepEqual(log.map((r) => [r.action, r.reason]), [["wake", "default"], ["skip", "duplicate"]]);
});

test("invalid config fails loudly with a location", () => {
  assert.throws(() => parseConfig("rules: [{ when: { path: a, eq: 1 }, then: skip }]"), /name/);
  assert.throws(() => parseConfig("rules: [{ name: x, when: { path: a, eq: 1, ne: 2 }, then: skip }]"), /exactly one operator/);
  assert.throws(() => parseConfig("rules: [{ name: x, when: { path: a, eq: 1 }, then: explode }]"), /one of wake, skip, batch/);
  assert.throws(() => parseConfig("budget: { quietHours: { start: '25:00', end: '07:00' } }"), /quietHours/);
  assert.throws(() => parseConfig("budget: { timezone: Mars/Base }"), /unknown zone/);
});

test("replay is a dry run and reports the cost difference", () => {
  const events = Array.from({ length: 48 }, (_, i) => ({ source: "heartbeat", key: "hb", ts: T0 + i * 30 * MIN, data: { ok: true } }));
  const r = replay(events, parseConfig("version: 1"), 1.4);
  assert.equal(r.counts.wake, 1);
  assert.equal(r.counts.skip, 47);
  assert.ok(Math.abs(r.gatedCost - 1.4) < 1e-9);
  assert.ok(Math.abs(r.baselineCost - 48 * 1.4) < 1e-9);
});
