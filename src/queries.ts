import type { DecisionRow, Store } from "./store.ts";
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
  /** Keys whose content changed since `since`, with the latest outcome. */
  changed: Array<{ source: string; key: string; changes: number; lastChange: string; lastAction: Action }>;
}

/** What changed while the agent was away. A by-product of the per-key state. */
export function catchup(store: Store, sinceMs: number): Catchup {
  const byKey = new Map<string, { source: string; key: string; changes: number; lastChange: number; lastAction: Action }>();
  for (const r of store.decisionsSince(sinceMs)) {
    if (!r.changed) continue;
    const id = `${r.source}\u0000${r.key}`;
    const cur = byKey.get(id);
    byKey.set(id, { source: r.source, key: r.key, changes: (cur?.changes ?? 0) + 1, lastChange: r.ts, lastAction: r.action });
  }
  return {
    since: new Date(sinceMs).toISOString(),
    pendingBatch: store.pendingCount(),
    changed: [...byKey.values()].map((v) => ({ ...v, lastChange: new Date(v.lastChange).toISOString() })),
  };
}

export function brief(r: DecisionRow) {
  return { id: r.id, ts: new Date(r.ts).toISOString(), source: r.source, key: r.key, action: r.action, reason: r.reason, event: r.event };
}
