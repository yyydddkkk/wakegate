import type { WakeEvent } from "../src/types.ts";

// Synthetic traffic with ground-truth labels, shared by the examples. All of it is the author's guess.
export const DAY = 86_400_000;
export const HOUR = 3_600_000;
export const MIN = 60_000;
export const DAYS = 14;
export const T0 = Date.UTC(2026, 0, 5);

export type Label = "must" | "nice" | "redundant" | "noise";
export interface Labeled {
  event: WakeEvent;
  label: Label;
}


function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function generate(seed: number): Labeled[] {
  const r = rng(seed);
  const poisson = (lambda: number) => {
    let l = Math.exp(-lambda), k = 0, p = 1;
    do { k++; p *= r(); } while (p > l);
    return k - 1;
  };
  const workHour = () => 9 * HOUR + r() * 9 * HOUR; // 09:00-18:00 UTC
  const anyHour = () => r() * DAY;
  const out: Labeled[] = [];
  const push = (ts: number, source: string, key: string, data: Record<string, unknown>, label: Label, severity?: string) =>
    out.push({ event: { source, key, ts: Math.round(ts), data, ...(severity ? { severity } : {}) }, label });

  let pr = 0, thread = 0, inc = 0;
  for (let d = 0; d < DAYS; d++) {
    const day = T0 + d * DAY;

    for (let i = 0; i < 48; i++) push(day + i * 30 * MIN, "heartbeat", "main", { status: "HEARTBEAT_OK", checkedAt: day + i * 30 * MIN }, "noise");

    // GitHub: human PRs with comment bursts, bot PRs, and some deploy-bot failures that matter.
    for (let i = poisson(8); i > 0; i--) {
      const late = r() < 0.1; // some threads happen in the evening, 21:00-24:00 UTC
      const t = day + (late ? 21 * HOUR + r() * 3 * HOUR : workHour());
      const key = `pr/${++pr}`;
      if (r() < 0.7) {
        const wantsReview = r() < 0.4;
        push(t, "github", key, { state: wantsReview ? "review_requested" : "opened", actor: "alice" }, wantsReview ? "must" : "nice");
        // Opened first, review requested a few minutes later on the same PR: the second one matters.
        if (!wantsReview && r() < 0.3) push(t + (2 + r() * 18) * MIN, "github", key, { state: "review_requested", actor: "alice" }, "must");
        let tc = t;
        for (let c = 0, n = 1 + Math.floor(r() * 5); c < n; c++) {
          tc += (1 + r() * 7) * MIN;
          push(tc, "github", key, { state: "commented", n: c, actor: "alice" }, c === 0 ? "nice" : "redundant");
        }
      } else if (r() < 0.08) {
        push(t, "github", key, { state: "deploy_failed", actor: "deploy-bot[bot]" }, "must");
      } else {
        push(t, "github", key, { state: "opened", actor: "dependabot[bot]" }, "noise");
      }
    }

    // Mail: mostly newsletters; some human; a few from a VIP that need action.
    for (let i = poisson(12); i > 0; i--) {
      const t = day + (r() < 0.8 ? workHour() : anyHour());
      const key = `thread/${++thread}`;
      const x = r();
      if (x < 0.65) push(t, "mail", key, { kind: "newsletter", subject: "Weekly digest" }, "noise");
      else if (x < 0.95) push(t, "mail", key, { kind: "human", subject: "Quick question" }, "nice");
      else push(t, "mail", key, { kind: "human", vip: true, subject: "Need your call today" }, "must");
    }

    // Pager: critical at any hour; identical re-alerts until acked; sometimes escalates.
    for (let i = poisson(0.6); i > 0; i--) {
      const t = day + anyHour();
      const key = `inc/${++inc}`;
      push(t, "pagerduty", key, { state: "triggered" }, "must", "critical");
      const repeats = 1 + Math.floor(r() * 3);
      for (let k = 1; k <= repeats; k++) push(t + k * 15 * MIN, "pagerduty", key, { state: "triggered" }, "redundant", "critical");
      if (r() < 0.3) push(t + (repeats + 1) * 15 * MIN, "pagerduty", key, { state: "escalated" }, "must", "critical");
    }

    // CI: runs are always "new" data; success is noise, main failures matter.
    for (let i = poisson(10), run = 0; i > 0; i--, run++) {
      const t = day + workHour();
      const onMain = r() < 0.4;
      const fail = r() < (onMain ? 0.06 : 0.2);
      push(t, "ci", onMain ? "main" : `branch/${Math.floor(r() * 5)}`, { result: fail ? "failure" : "success", branch: onMain ? "main" : "feature", run: d * 100 + run },
        fail ? (onMain ? "must" : "nice") : "noise");
    }
  }
  return out.sort((a, b) => a.event.ts! - b.event.ts!);
}

