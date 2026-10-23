import { describeConfig, type Config } from "./config.ts";
import { dayOf, inQuietHours } from "./gate.ts";
import type { Store } from "./store.ts";
import type { Action } from "./types.ts";

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
    if (!opts.includeFiltered && r.action === "skip" && r.reason.startsWith("rule:")) {
      filtered++;
      continue;
    }
    const id = `${r.source}\u0000${r.key}`;
    const cur = byKey.get(id);
    byKey.set(id, { source: r.source, key: r.key, changes: (cur?.changes ?? 0) + 1, lastChange: r.ts, lastAction: r.action });
  }
  return {
    since: new Date(sinceMs).toISOString(),
    pendingBatch: store.pendingCount(),
    filteredBySkipRules: filtered,
    changed: [...byKey.values()].map((v) => ({ ...v, lastChange: new Date(v.lastChange).toISOString() })),
  };
}

export { brief } from "./store.ts";

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
