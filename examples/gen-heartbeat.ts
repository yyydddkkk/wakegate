// Generates a synthetic week of an always-on agent: a heartbeat every 30 minutes
// (almost always "nothing to do") plus a handful of real events.
// Usage: node examples/gen-heartbeat.ts > examples/heartbeat.jsonl
const start = Date.UTC(2026, 0, 5, 0, 0, 0);
const HALF_HOUR = 30 * 60_000;
const out: unknown[] = [];

for (let i = 0; i < 7 * 48; i++) {
  const ts = start + i * HALF_HOUR;
  // The heartbeat payload only differs by a timestamp the agent would never care about.
  out.push({ source: "heartbeat", key: "main", ts, data: { status: "HEARTBEAT_OK", checkedAt: ts } });
}

const real = [
  [1, 9, "github", "pr/412", { state: "review_requested", actor: "alice" }],
  [1, 9.2, "github", "pr/412", { state: "review_requested", actor: "ci[bot]" }],
  [1, 14, "mail", "thread/88", { subject: "Invoice question", unread: 1 }],
  [2, 3, "pagerduty", "inc/7", { state: "triggered" }, "critical"],
  [3, 10, "github", "issue/90", { state: "opened", actor: "bob" }],
  [3, 10.1, "github", "issue/90", { state: "labeled", actor: "bob" }],
  [3, 10.2, "github", "issue/90", { state: "commented", actor: "bob" }],
  [5, 16, "mail", "thread/91", { subject: "Contract draft", unread: 1 }],
] as const;
for (const [day, hour, source, key, data, severity] of real) {
  out.push({ source, key, ts: start + day * 86_400_000 + hour * 3_600_000, data, ...(severity ? { severity } : {}) });
}

for (const e of out) console.log(JSON.stringify(e));
