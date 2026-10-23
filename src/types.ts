import type { Brief } from "./store.ts";

export type Action = "wake" | "skip" | "batch";

/** The standard envelope every source adapter produces. */
export interface WakeEvent {
  /** Where the event came from, e.g. "github", "heartbeat", a hostname. */
  source: string;
  /** Stable identity of the thing that changed. State is tracked per (source, key). */
  key: string;
  /** Epoch milliseconds. Defaults to "now"; replay requires it. */
  ts?: number;
  /** Optional hint rules can match on, e.g. "critical". */
  severity?: string;
  /** Arbitrary payload. Hashed for change detection, never stored by the fingerprint. */
  data?: unknown;
}

export interface Decision {
  action: Action;
  /** Machine-readable reason, e.g. "duplicate", "bypass:security", "daily-budget". */
  reason: string;
  /** Name of the rule that decided, if any. */
  rule?: string;
  source: string;
  key: string;
  ts: number;
  fingerprint: string;
  /** True when the fingerprint differs from the last one seen for this key. */
  changed: boolean;
  /** Row id in the audit log. */
  id: number;
  /** On a wake with `flushOnWake`: events batched earlier, delivered now (once). */
  batch?: Brief[];
}
