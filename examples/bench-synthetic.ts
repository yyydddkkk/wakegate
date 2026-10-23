// Synthetic stress test: does the gate lose events that matter?
//
// Every event carries a ground-truth label written by this script, so misses
// can be counted. That is also the weakness: the traffic mix, the labels and
// the traps below are all the author's guesses. The result shows WHICH
// mechanisms can lose events and by roughly how much under these patterns.
// It does not estimate real-world rates.
//
//   node examples/bench-synthetic.ts [seeds=5]

import { readFileSync } from "node:fs";
import { parseConfig } from "../src/config.ts";
import { Gate } from "../src/gate.ts";
import { Store } from "../src/store.ts";

import { DAY, DAYS, generate, HOUR, MIN, T0, type Label, type Labeled } from "./synthetic-traffic.ts";

const NAIVE = "dedupe: false";

// BEFORE: the example config as it shipped in the first commits, and the old semantics
// (no exempt, batches only delivered by an explicit flush).
const SHIPPED_BEFORE = `
flushOnWake: false
ignorePaths: [checkedAt]
bypass: [{ name: critical, when: { path: severity, eq: critical } }]
rules: [{ name: ignore-bots, when: { path: data.actor, matches: "\\\\[bot\\\\]$" }, then: skip }]
cooldownSeconds: 1800
budget: { maxWakesPerDay: 10, overBudget: batch, quietHours: { start: "22:00", end: "07:00" }, timezone: UTC }
`;
// AFTER: the example config as it ships now.
const SHIPPED_AFTER = readFileSync(new URL("./wakegate.yaml", import.meta.url), "utf8");

// A config tuned to this traffic. `after` adds the new features: exempt for review requests and flushOnWake.
const tuned = (extra: string, after: boolean) => `
flushOnWake: ${after}
ignorePaths: [checkedAt]
bypass:
  - { name: critical, when: { path: severity, eq: critical } }
  - { name: deploy-failed, when: { path: data.state, eq: deploy_failed } }
  - { name: vip-mail, when: { path: data.vip, eq: true } }
  - { name: main-ci-failure, when: { all: [{ path: key, eq: main }, { path: data.result, eq: failure }] } }
rules:
${after ? "  - { name: review-requests, when: { path: data.state, eq: review_requested }, then: wake, exempt: [cooldown, budget] }\n" : ""}  - { name: newsletters, when: { path: data.kind, eq: newsletter }, then: skip }
  - { name: ci-success, when: { path: data.result, eq: success }, then: skip }
  - { name: heartbeat, when: { path: source, eq: heartbeat }, then: skip }
  - { name: ignore-bots, when: { path: data.actor, matches: "\\\\[bot\\\\]$" }, then: skip }
${extra}
`;
const QUIET = "quietHours: { start: '22:00', end: '07:00' }, timezone: UTC";
const FULL = (after: boolean, over: string) => tuned(`cooldownSeconds: 1800\nbudget: { maxWakesPerDay: 10, ${over}${QUIET} }`, after);

const CONFIGS: Array<[string, string]> = [
  ["naive: wake on everything", NAIVE],
  ["BEFORE shipped example", SHIPPED_BEFORE],
  ["AFTER  shipped example", SHIPPED_AFTER],
  ["BEFORE tuned, cooldown only", tuned("cooldownSeconds: 1800", false)],
  ["AFTER  tuned, cooldown only", tuned("cooldownSeconds: 1800", true)],
  ["BEFORE tuned, full (batch)", FULL(false, "")],
  ["AFTER  tuned, full (batch)", FULL(true, "")],
  ["BEFORE tuned, full (skip)", FULL(false, "overBudget: skip, ")],
  ["AFTER  tuned, full (skip)", FULL(true, "overBudget: skip, ")],
];

type Flush = "daily-08:00" | "never";

function run(events: Labeled[], yaml: string, flush: Flush) {
  const store = new Store(":memory:");
  const gate = new Gate(parseConfig(yaml), store);
  const labelById = new Map<number, Label>();
  const tsById = new Map<number, number>();
  const delivered = new Map<number, number>(); // decision id -> delivery time
  const kind = new Map<number, "wake" | "skip">();
  const reasonById = new Map<number, string>();
  let wakes = 0, lastFlushDay = -1;

  const doFlush = (at: number) => {
    for (const row of store.takePending()) delivered.set(row.id, at);
  };

  for (const { event, label } of events) {
    const ts = event.ts!;
    if (flush !== "never") {
      const day = Math.floor((ts - T0) / DAY);
      if (day > lastFlushDay && ts - T0 - day * DAY >= 8 * HOUR) {
        doFlush(T0 + day * DAY + 8 * HOUR);
        lastFlushDay = day;
      }
    }
    const d = gate.decide(event);
    labelById.set(d.id, label);
    tsById.set(d.id, ts);
    reasonById.set(d.id, `${d.action}:${d.reason}`);
    if (d.action === "wake") {
      wakes++;
      delivered.set(d.id, ts);
      kind.set(d.id, "wake");
      for (const b of d.batch ?? []) delivered.set(b.id, ts); // flushOnWake: batch rides along
    } else if (d.action === "skip") kind.set(d.id, "skip");
  }
  store.close();

  const must = [...labelById].filter(([, l]) => l === "must").map(([id]) => id);
  const nice = [...labelById].filter(([, l]) => l === "nice").map(([id]) => id);
  const within = (ids: number[], ms: number) => ids.filter((id) => delivered.has(id) && delivered.get(id)! - tsById.get(id)! <= ms).length;
  const pct = (n: number, d: number) => (d ? (100 * n) / d : 100);
  const wasted = [...labelById].filter(([id, l]) => kind.get(id) === "wake" && (l === "noise" || l === "redundant")).length;
  const delays = must.filter((id) => delivered.has(id)).map((id) => delivered.get(id)! - tsById.get(id)!).sort((a, b) => a - b);
  const lost: Record<string, number> = {};
  const late: Record<string, number> = {};
  for (const id of must) {
    const t = delivered.get(id);
    if (t === undefined) lost[reasonById.get(id)!] = (lost[reasonById.get(id)!] ?? 0) + 1;
    else if (t - tsById.get(id)! > 0) late[reasonById.get(id)!] = (late[reasonById.get(id)!] ?? 0) + 1;
  }
  return {
    lost,
    late,
    total: events.length,
    wakes,
    mustN: must.length,
    mustNow: pct(within(must, 0), must.length),
    must1h: pct(within(must, HOUR), must.length),
    mustEver: pct(must.filter((id) => delivered.has(id)).length, must.length),
    niceEver: pct(nice.filter((id) => delivered.has(id)).length, nice.length),
    wasted,
    p95delayMin: delays.length ? delays[Math.floor(delays.length * 0.95)]! / MIN : 0,
  };
}

const seeds = Number(process.argv[2] ?? 5);
const datasets = Array.from({ length: seeds }, (_, i) => generate(1000 + i));
const avg = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;
const f = (n: number, w = 5, d = 1) => n.toFixed(d).padStart(w);

console.log(`${DAYS} days x ${seeds} seeds. Avg ${Math.round(avg(datasets.map((d) => d.length)))} events and ${Math.round(avg(datasets.map((d) => d.filter((e) => e.label === "must").length)))} must-wake events per run.`);
console.log("Columns: wakes/run | must-wake delivered: instantly / within 1h / ever (worst seed) | nice-to-know ever | wasted wakes (noise+redundant) | p95 delay of must-wake delivered (min)\n");

const breakdown: Array<[string, Array<ReturnType<typeof run>>]> = [];
for (const flush of ["never", "daily-08:00"] as Flush[]) {
  console.log(`--- batch delivery: ${flush === "never" ? "the agent never calls flush (only flushOnWake can deliver a batch)" : "the agent also flushes by hand daily at 08:00 UTC"} ---`);
  for (const [name, yaml] of CONFIGS) {
    const rs = datasets.map((d) => run(d, yaml, flush));
    if (flush === "never") breakdown.push([name, rs]);
    const worst = Math.min(...rs.map((x) => x.mustEver));
    console.log(
      `${name.padEnd(28)} ${f(avg(rs.map((x) => x.wakes)), 6, 0)} | ${f(avg(rs.map((x) => x.mustNow)))}% ${f(avg(rs.map((x) => x.must1h)))}% ${f(avg(rs.map((x) => x.mustEver)))}% (${f(worst)}%) | ${f(avg(rs.map((x) => x.niceEver)))}% | ${f(avg(rs.map((x) => x.wasted)), 5, 0)} | ${f(avg(rs.map((x) => x.p95delayMin)), 6, 0)}`,
    );
  }
  console.log();
}

console.log("--- why must-wake events did not wake instantly (agent never flushes by hand; summed over seeds) ---");
const sum = (rs: Array<ReturnType<typeof run>>, k: "lost" | "late") => {
  const t: Record<string, number> = {};
  for (const x of rs) for (const [a, n] of Object.entries(x[k])) t[a] = (t[a] ?? 0) + n;
  return Object.entries(t).sort((a, b) => b[1] - a[1]).map(([a, n]) => `${a} x${n}`).join(", ") || "none";
};
for (const [name, rs] of breakdown) {
  if (name.startsWith("naive")) continue;
  console.log(`${name.trim()}\n    never delivered: ${sum(rs, "lost")}\n    delivered late:  ${sum(rs, "late")}`);
}
