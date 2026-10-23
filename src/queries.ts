import { describeConfig, type Config } from "./config.ts";
import { dayOf, inQuietHours, isCanaryCandidate } from "./gate.ts";
import { seededRandom } from "./hash.ts";
import { brief, type Brief, type DecisionRow, type Store } from "./store.ts";
import type { Action, Mode } from "./types.ts";

/** "24h", "30m", "7d", "90s" or an ISO date -> epoch ms. */
export function parseSince(v: string, now = Date.now()): number {
  const m = /^(\d+)([smhd])$/.exec(v);
  if (m) return now - Number(m[1]) * { s: 1e3, m: 6e4, h: 36e5, d: 864e5 }[m[2] as "s" | "m" | "h" | "d"];
  const t = Date.parse(v);
  if (Number.isNaN(t)) throw new Error(`expected a duration like 24h or an ISO date, got "${v}"`);
  return t;
}

export interface Catchup {
  since: string;
  /** Events waiting in the batch. Call flush to take them. */
  pendingBatch: number;
  /** Changes dropped by a `skip` rule. Hidden from `changed` unless includeFiltered is set. */
  filteredBySkipRules: number;
  /** Keys whose content changed since `since`, with the latest outcome. */
  changed: Array<{ source: string; key: string; changes: number; lastChange: string; lastAction: Action }>;
}

/** What changed while the agent was away. A by-product of the per-key state. */
export function catchup(store: Store, sinceMs: number, opts: { includeFiltered?: boolean } = {}): Catchup {
  const byKey = new Map<string, { source: string; key: string; changes: number; lastChange: number; lastAction: Action }>();
  let filtered = 0;
  for (const r of store.decisionsSince(sinceMs)) {
    if (!r.changed) continue;
    // A skip rule is the user saying "I don't care about this"; don't hand it back as news.
    if (!opts.includeFiltered && r.verdict === "skip" && r.reason.startsWith("rule:")) {
      filtered++;
      continue;
    }
    const id = `${r.source}\u0000${r.key}`;
    const cur = byKey.get(id);
    byKey.set(id, { source: r.source, key: r.key, changes: (cur?.changes ?? 0) + 1, lastChange: r.ts, lastAction: r.verdict });
  }
  return {
    since: new Date(sinceMs).toISOString(),
    pendingBatch: store.pendingCount(),
    filteredBySkipRules: filtered,
    changed: [...byKey.values()].map((v) => ({ ...v, lastChange: new Date(v.lastChange).toISOString() })),
  };
}

export { brief };

/** The active policy plus where today stands against it. */
export function rules(config: Config, store: Store, now = Date.now()) {
  const { quietHours, maxWakesPerDay, timezone } = config.budget;
  const wakesToday = store.countWakes(dayOf(now, timezone));
  return {
    ...describeConfig(config),
    now: {
      time: new Date(now).toISOString(),
      inQuietHours: quietHours ? inQuietHours(now, quietHours.start, quietHours.end, timezone) : false,
      wakesToday,
      wakesRemainingToday: maxWakesPerDay === undefined ? null : Math.max(0, maxWakesPerDay - wakesToday),
      pendingBatch: store.pendingCount(),
    },
  };
}

const isSuppressed = (r: DecisionRow) => isCanaryCandidate({ action: r.verdict, reason: r.reason });

/** 95% Wilson score interval for k successes in n trials. */
export function wilson(k: number, n: number): [number, number] {
  if (n === 0) return [0, 1];
  const z = 1.96, p = k / n, d = 1 + (z * z) / n;
  const c = (p + (z * z) / (2 * n)) / d;
  const h = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / d;
  return [Math.max(0, c - h), Math.min(1, c + h)];
}

export interface Report {
  since: string;
  events: number;
  /** What the rules decided, regardless of what was delivered. */
  verdicts: Record<Action, number>;
  byMode: Record<Mode, number>;
  topReasons: Array<{ verdict: Action; reason: string; n: number }>;
  /** Wakes the rules would have avoided (shadow mode), and what that is worth. */
  wakesAvoidable: number;
  estimatedSaving: number | null;
  /** Skips other than duplicates: the population a missed event can hide in. */
  suppressedByRules: number;
  labels: { total: number; wanted: number; unwanted: number };
  /** Wakes the rules approved but a person or agent called unwanted. */
  unwantedWakes: number;
  /** Random samples (canaries, review queue) waiting for a label. */
  unlabeledSamples: number;
  /** Estimated share of rule-suppressed events that were wanted. null until there is a labelled sample. */
  missEstimate: null | {
    sampleSize: number;
    wanted: number;
    rate: number;
    ci95: [number, number];
    /** Rate interval times the rule-suppressed events that were not already delivered as canaries. In shadow mode: events enforcing would have missed. */
    expectedMissedEvents: [number, number];
  };
  /** The same estimate per suppressing rule: the way to find which rule is hiding wanted events. */
  byRule: Array<{ rule: string; suppressed: number; sampleSize: number; wanted: number; ci95: [number, number] }>;
  advice: string[];
}

export function report(store: Store, sinceMs: number, opts: { costPerWake?: number; minSample?: number } = {}): Report {
  const rows = store.decisionsSince(sinceMs);
  const labels = store.labels();
  const verdicts: Record<Action, number> = { wake: 0, skip: 0, batch: 0 };
  const byMode: Record<Mode, number> = { enforce: 0, shadow: 0, canary: 0 };
  const reasons = new Map<string, number>();
  let suppressed = 0, suppressedUndelivered = 0, shadowAvoidable = 0, unwantedWakes = 0, unlabeled = 0;
  const sample = { n: 0, wanted: 0 };
  const perRule = new Map<string, { suppressed: number; n: number; wanted: number }>();
  const queued = new Set(store.queuedIds());

  for (const r of rows) {
    verdicts[r.verdict]++;
    byMode[r.mode]++;
    const k = `${r.verdict}\u0000${r.reason}`;
    reasons.set(k, (reasons.get(k) ?? 0) + 1);
    if (r.mode === "shadow" && r.verdict !== "wake") shadowAvoidable++;
    const l = labels.get(r.id);
    if (isSuppressed(r)) {
      suppressed++;
      if (r.mode !== "canary") suppressedUndelivered++;
      const isSample = r.mode === "canary" || queued.has(r.id);
      const bucket = perRule.get(r.rule ?? r.reason) ?? { suppressed: 0, n: 0, wanted: 0 };
      bucket.suppressed++;
      perRule.set(r.rule ?? r.reason, bucket);
      if (isSample && !l) unlabeled++;
      if (isSample && l?.sample) {
        sample.n++;
        bucket.n++;
        if (l.label === "wanted") {
          sample.wanted++;
          bucket.wanted++;
        }
      }
    }
    if (l && l.label === "unwanted" && r.verdict === "wake") unwantedWakes++;
  }

  const lab = { total: 0, wanted: 0, unwanted: 0 };
  for (const r of rows) {
    const l = labels.get(r.id);
    if (!l) continue;
    lab.total++;
    lab[l.label]++;
  }

  let missEstimate: Report["missEstimate"] = null;
  if (sample.n > 0) {
    const [lo, hi] = wilson(sample.wanted, sample.n);
    missEstimate = {
      sampleSize: sample.n,
      wanted: sample.wanted,
      rate: sample.wanted / sample.n,
      ci95: [lo, hi],
      expectedMissedEvents: [Math.round(lo * suppressedUndelivered), Math.round(hi * suppressedUndelivered)],
    };
  }

  const minSample = opts.minSample ?? 30;
  const advice: string[] = [];
  if (byMode.shadow > 0) advice.push("Shadow mode: nothing was withheld. Label a sample with `wakegate review --sample 30` to learn what enforcing would have cost.");
  if (suppressed > 0 && sample.n < minSample) {
    advice.push(`Only ${sample.n} labelled random samples; at least ~${minSample} are needed before the miss estimate means much. ${suppressed} events were suppressed by rules.`);
  }
  if (sample.n > 0 && sample.wanted === 0) advice.push(`No wanted events in ${sample.n} samples. That bounds the miss rate (see the interval) but cannot rule out rare misses: a 1% miss rate needs hundreds of labels to see.`);
  if (unlabeled > 0) advice.push(`${unlabeled} sampled events are waiting for a label (wakegate review).`);

  return {
    since: new Date(sinceMs).toISOString(),
    events: rows.length,
    verdicts,
    byMode,
    topReasons: [...reasons].map(([k, n]) => ({ verdict: k.split("\u0000")[0] as Action, reason: k.split("\u0000")[1]!, n })).sort((a, b) => b.n - a.n).slice(0, 8),
    wakesAvoidable: shadowAvoidable,
    estimatedSaving: opts.costPerWake ? shadowAvoidable * opts.costPerWake : null,
    suppressedByRules: suppressed,
    labels: lab,
    unwantedWakes,
    unlabeledSamples: unlabeled,
    missEstimate,
    byRule: [...perRule]
      .map(([rule, b]) => ({ rule, suppressed: b.suppressed, sampleSize: b.n, wanted: b.wanted, ci95: wilson(b.wanted, b.n) }))
      .sort((a, b) => b.wanted - a.wanted || b.suppressed - a.suppressed),
    advice,
  };
}

/**
 * Draws a uniform random sample of rule-suppressed events (skips other than duplicates)
 * for a person to label, and returns everything still waiting for a label:
 * the new draw, earlier draws and unlabeled canaries.
 */
export function review(store: Store, sinceMs: number, opts: { sample?: number; seed?: number; now?: number } = {}): Brief[] {
  const labels = store.labels();
  const queued = new Set(store.queuedIds());
  const rows = store.decisionsSince(sinceMs);

  if (opts.sample && opts.sample > 0) {
    const pool = rows.filter((r) => isSuppressed(r) && r.mode !== "canary" && !queued.has(r.id) && !labels.has(r.id));
    const rand = seededRandom(opts.seed ?? Date.now());
    for (let i = pool.length - 1; i > 0; i--) {
      const j = Math.floor(rand() * (i + 1));
      [pool[i], pool[j]] = [pool[j]!, pool[i]!];
    }
    const picked = pool.slice(0, opts.sample);
    store.addToQueue(picked.map((r) => r.id), opts.now ?? Date.now());
    for (const r of picked) queued.add(r.id);
  }
  return rows.filter((r) => !labels.has(r.id) && (queued.has(r.id) || (r.mode === "canary" && isSuppressed(r)))).map(brief);
}
