import { readFileSync } from "node:fs";
import type { Config } from "./config.ts";
import { Gate } from "./gate.ts";
import { Store } from "./store.ts";
import type { Action, WakeEvent } from "./types.ts";

export interface ReplayResult {
  total: number;
  counts: Record<Action, number>;
  reasons: Record<string, number>;
  /** Estimated cost if every event woke the agent vs. only the wakes. */
  baselineCost: number;
  gatedCost: number;
  /** Skips released as canaries (they cost a wake each and are included in gatedCost). */
  canariesReleased: number;
  /** Batched events that rode along with a later wake (flushOnWake). */
  batchDelivered: number;
  /** Batched events still waiting when the events ran out. */
  batchStranded: number;
}

export function readEvents(path: string): WakeEvent[] {
  const events = readFileSync(path, "utf8")
    .split("\n")
    .filter((l) => l.trim())
    .map((l, i) => {
      try {
        return JSON.parse(l) as WakeEvent;
      } catch {
        throw new Error(`${path}:${i + 1}: invalid JSON`);
      }
    });
  events.forEach((e, i) => {
    if (typeof e.ts !== "number") throw new Error(`${path}: event ${i + 1} has no numeric "ts" (required for replay)`);
  });
  return events.sort((a, b) => a.ts! - b.ts!);
}

/**
 * Runs events through the gate against a fresh in-memory state, using each
 * event's own timestamp as the clock. Nothing on disk is touched.
 */
export function replay(events: WakeEvent[], config: Config, costPerWake = 0): ReplayResult {
  const store = new Store(":memory:");
  try {
    const gate = new Gate(config, store);
    const counts: Record<Action, number> = { wake: 0, skip: 0, batch: 0 };
    const reasons: Record<string, number> = {};
    let batchDelivered = 0, canaries = 0;
    for (const e of events) {
      const d = gate.decide(e);
      batchDelivered += d.batch?.length ?? 0;
      if (d.mode === "canary") canaries++;
      // Count what the rules decided, so a shadow-mode config shows what enforcing would do.
      counts[d.verdict]++;
      reasons[`${d.verdict}:${d.reason}`] = (reasons[`${d.verdict}:${d.reason}`] ?? 0) + 1;
    }
    return {
      total: events.length,
      counts,
      reasons,
      baselineCost: events.length * costPerWake,
      gatedCost: (counts.wake + canaries) * costPerWake,
      canariesReleased: canaries,
      batchDelivered,
      batchStranded: store.pendingCount(),
    };
  } finally {
    store.close();
  }
}
