import type { Config } from "./config.ts";
import { canonicalJson, omitPaths, sha256 } from "./hash.ts";
import { brief, type Store } from "./store.ts";
import type { Action, Decision, Mode, WakeEvent } from "./types.ts";

export interface GateOptions {
  /** Clock used when an event has no `ts`. Defaults to Date.now. */
  now?: () => number;
}

export interface Verdict {
  action: Action;
  reason: string;
  rule?: string;
}

/**
 * Decision order, first hit wins:
 *   1. duplicate     same fingerprint as last time for this key  -> skip
 *   2. bypass        matches a bypass rule                       -> wake (ignores 4 and 5)
 *   3. rules         first matching rule, else the default
 *   4. cooldown      woke for this key too recently              -> batch
 *   5. quiet hours / daily budget                                -> batch or skip
 * A matching rule can list `exempt: [cooldown, quietHours, budget]` to skip some of 4 and 5.
 * On a wake with flushOnWake (default), batched events ride along in `batch`.
 * Every decision, including skips, is written to the audit log.
 */
export class Gate {
  private config: Config;
  private store: Store;
  private opts: GateOptions;

  constructor(config: Config, store: Store, opts: GateOptions = {}) {
    this.config = config;
    this.store = store;
    this.opts = opts;
  }

  decide(event: WakeEvent): Decision {
    const ts = event.ts ?? (this.opts.now ?? Date.now)();
    const ev: WakeEvent = { ...event, ts };
    const fingerprint = sha256(canonicalJson({ s: ev.severity ?? null, d: omitPaths(ev.data, this.config.ignorePaths) }));
    const tz = this.config.budget.timezone;

    return this.store.transaction(() => {
      const prev = this.store.getState(ev.source, ev.key);
      const changed = prev?.fingerprint !== fingerprint;
      const verdict = this.evaluate(ev, ts, changed, prev?.lastWakeTs ?? null);

      // What the caller is told to do can differ from the verdict (shadow, canary).
      let mode: Mode = "enforce";
      let delivered: Action = verdict.action;
      if (this.config.mode === "shadow") {
        mode = "shadow";
        delivered = "wake";
      } else if (isCanaryCandidate(verdict) && canaryRoll(ev, fingerprint) < this.config.canaryRate) {
        mode = "canary";
        delivered = "wake";
      }

      const carried = delivered === "wake" && this.config.flushOnWake ? this.store.takePending().map(brief) : [];

      const id = this.store.insertDecision({
        ts,
        day: dayOf(ts, tz),
        source: ev.source,
        key: ev.key,
        fingerprint,
        action: delivered,
        verdict: verdict.action,
        mode,
        reason: verdict.reason,
        rule: verdict.rule ?? null,
        changed: changed ? 1 : 0,
        event: ev,
      });
      // Cooldown and budget follow the verdict, so shadow mode simulates enforcement faithfully
      // and a canary does not spend the budget.
      this.store.putState(ev.source, ev.key, fingerprint, ts, verdict.action === "wake");
      if (delivered === "batch") this.store.addPending(id);
      return {
        ...verdict,
        action: delivered,
        verdict: verdict.action,
        mode,
        source: ev.source,
        key: ev.key,
        ts,
        fingerprint,
        changed,
        id,
        ...(mode === "canary"
          ? {
              canary: true as const,
              note: `Released as a canary: your rules would have skipped this (${verdict.reason}). Record whether it was worth waking for: wakegate label ${id} wanted|unwanted.`,
            }
          : {}),
        ...(carried.length ? { batch: carried } : {}),
      };
    });
  }

  private evaluate(ev: WakeEvent, ts: number, changed: boolean, lastWakeTs: number | null): Verdict {
    const c = this.config;
    if (c.dedupe && !changed) return { action: "skip", reason: "duplicate" };

    const bypass = c.bypass.find((r) => r.when(ev));
    if (bypass) return { action: "wake", reason: `bypass:${bypass.name}`, rule: bypass.name };

    const hit = c.rules.find((r) => r.when(ev));
    const exempt = new Set(hit?.exempt);
    const proposed: Verdict = hit
      ? { action: hit.then, reason: `rule:${hit.name}`, rule: hit.name }
      : { action: c.defaultAction, reason: "default" };
    if (proposed.action !== "wake") return proposed;

    if (!exempt.has("cooldown") && c.cooldownSeconds > 0 && lastWakeTs !== null && ts - lastWakeTs < c.cooldownSeconds * 1000) {
      return { action: "batch", reason: "cooldown", rule: proposed.rule };
    }
    const { quietHours, maxWakesPerDay, overBudget, timezone } = c.budget;
    if (!exempt.has("quietHours") && quietHours && inQuietHours(ts, quietHours.start, quietHours.end, timezone)) {
      return { action: "batch", reason: "quiet-hours", rule: proposed.rule };
    }
    if (!exempt.has("budget") && maxWakesPerDay !== undefined && this.store.countWakes(dayOf(ts, timezone)) >= maxWakesPerDay) {
      return { action: overBudget, reason: "daily-budget", rule: proposed.rule };
    }
    return proposed;
  }
}

function parts(ts: number, tz?: string): { day: string; minutes: number } {
  const f = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
  const p = Object.fromEntries(f.formatToParts(ts).map((x) => [x.type, x.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, minutes: Number(p.hour) * 60 + Number(p.minute) };
}

export function dayOf(ts: number, tz?: string): string {
  return parts(ts, tz).day;
}

function toMinutes(hhmm: string): number {
  const [h, m] = hhmm.split(":").map(Number);
  return h! * 60 + m!;
}

/** Window may wrap midnight, e.g. 22:00-07:00. start === end means never. */
export function inQuietHours(ts: number, start: string, end: string, tz?: string): boolean {
  const s = toMinutes(start);
  const e = toMinutes(end);
  if (s === e) return false;
  const now = parts(ts, tz).minutes;
  return s < e ? now >= s && now < e : now >= s || now < e;
}

/** A skip that carries information. Duplicates are excluded: they add nothing worth a wake. */
export function isCanaryCandidate(v: Verdict): boolean {
  return v.action === "skip" && v.reason !== "duplicate";
}

/** Deterministic value in [0, 1) per event, so replays and tests are reproducible. */
function canaryRoll(ev: WakeEvent, fingerprint: string): number {
  const h = sha256(`${ev.source}\u0000${ev.key}\u0000${fingerprint}\u0000${ev.ts}`);
  return parseInt(h.slice(0, 8), 16) / 0x1_0000_0000;
}
