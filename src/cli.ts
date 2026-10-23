#!/usr/bin/env node
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { parseArgs } from "node:util";
import { loadConfig, type Config } from "./config.ts";
import { Gate } from "./gate.ts";
import { pollUrl } from "./poll.ts";
import { serveStdio } from "./mcp.ts";
import { brief, catchup, parseSince, rules } from "./queries.ts";
import { readEvents, replay } from "./replay.ts";
import { Store } from "./store.ts";
import type { Action, WakeEvent } from "./types.ts";

/** Exit codes: scripts can branch with `if wakegate check ...; then wake_agent; fi`. */
const EXIT: Record<Action, number> = { wake: 0, skip: 10, batch: 11 };

const HELP = `wakegate - decide whether an event should wake your agent

  wakegate check  [--event JSON]   read one event (stdin if no --event), print the decision
                                   exit: 0 wake | 10 skip | 11 batch | 1 error
  wakegate poll   <url>            one conditional GET, then check the result
  wakegate flush                   print and clear the batched events as JSON
  wakegate catchup [--since 24h]   what changed since then (per key); --include-filtered adds
                                   changes your skip rules dropped
  wakegate rules                   the active policy and where today stands against it
  wakegate log    [--limit 50]     audit log: every decision with its reason
  wakegate replay <events.jsonl>   dry run against a fresh state; nothing is written
  wakegate mcp                     serve the gate as MCP tools over stdio
  wakegate init                    write a starter wakegate.yaml

options: --config wakegate.yaml  --db .wakegate/state.db  --source NAME
         --cost-per-wake N       (replay) estimated cost of one agent wake
`;

function fail(msg: string): never {
  console.error(`wakegate: ${msg}`);
  process.exit(1);
}

const STARTER = `version: 1

# Drop fields that change on every event but mean nothing.
ignorePaths: [fetchedAt, meta.requestId]

# Events matching these always wake. They ignore cooldown, quiet hours and budget.
bypass:
  - name: critical
    when: { path: severity, eq: critical }

# First matching rule decides, so ORDER MATTERS. A skip rule hides everything it
# matches, including events you would want: put exceptions above it.
rules:
  # Above ignore-bots on purpose: a deploy bot failing is not noise.
  - name: deploy-failures
    when: { path: data.state, eq: deploy_failed }
    then: wake
    exempt: [cooldown, quietHours, budget]
  - name: ignore-bots
    when: { path: data.actor, matches: "\\\\[bot\\\\]$" }
    then: skip
  # Important changes should not wait behind a cooldown or an exhausted budget.
  - name: review-requests
    when: { path: data.state, eq: review_requested }
    then: wake
    exempt: [cooldown, budget]
  - name: heartbeat-is-noise
    when: { path: source, eq: heartbeat }
    then: skip

default: wake
cooldownSeconds: 300        # after waking for a key, batch further changes for 5 min
flushOnWake: true           # a wake also hands over everything batched earlier (default)

budget:
  maxWakesPerDay: 20
  overBudget: batch          # or "skip"; "skip" drops events for good
  quietHours: { start: "22:00", end: "07:00" }
  # timezone: Asia/Shanghai
`;

async function main(): Promise<void> {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      config: { type: "string", default: "wakegate.yaml" },
      db: { type: "string", default: ".wakegate/state.db" },
      event: { type: "string" },
      source: { type: "string" },
      since: { type: "string", default: "24h" },
      limit: { type: "string", default: "50" },
      "cost-per-wake": { type: "string", default: "0" },
      "include-filtered": { type: "boolean", default: false },
      help: { type: "boolean", short: "h" },
    },
  });
  const [cmd, arg] = positionals;
  if (values.help || !cmd) {
    console.log(HELP);
    return;
  }

  if (cmd === "init") {
    if (existsSync(values.config)) fail(`${values.config} already exists`);
    writeFileSync(values.config, STARTER);
    console.log(`wrote ${values.config}`);
    return;
  }

  const loadCfg = (): Config => {
    if (!existsSync(values.config)) fail(`${values.config} not found (run "wakegate init")`);
    return loadConfig(values.config);
  };

  if (cmd === "replay") {
    if (!arg) fail("replay needs an events file (JSON lines with a numeric ts)");
    const cost = Number(values["cost-per-wake"]);
    const r = replay(readEvents(arg), loadCfg(), cost);
    console.log(`events: ${r.total}`);
    console.log(`wake: ${r.counts.wake}   skip: ${r.counts.skip}   batch: ${r.counts.batch}`);
    for (const [reason, n] of Object.entries(r.reasons).sort((a, b) => b[1] - a[1])) console.log(`  ${String(n).padStart(6)}  ${reason}`);
    if (cost > 0) {
      const saved = r.baselineCost - r.gatedCost;
      console.log(`cost: ${r.baselineCost.toFixed(2)} -> ${r.gatedCost.toFixed(2)}  (saved ${saved.toFixed(2)}, ${((saved / r.baselineCost) * 100).toFixed(1)}%)`);
      if (r.counts.batch) console.log(`batched: ${r.batchDelivered} delivered with a later wake (no extra cost), ${r.batchStranded} still waiting at the end of the log.`);
    }
    return;
  }

  const store = new Store(values.db);
  try {
    switch (cmd) {
      case "check":
      case "poll": {
        const gate = new Gate(loadCfg(), store);
        let event: WakeEvent;
        if (cmd === "poll") {
          if (!arg) fail("poll needs a URL");
          const r = await pollUrl(arg, store, { source: values.source });
          if (r.kind === "not-modified") {
            console.log(JSON.stringify({ action: "skip", reason: "not-modified", pollIntervalHint: r.pollIntervalHint }));
            process.exitCode = EXIT.skip;
            return;
          }
          event = r.event;
        } else {
          const text = values.event ?? readFileSync(0, "utf8");
          try {
            event = JSON.parse(text) as WakeEvent;
          } catch {
            fail("event is not valid JSON");
          }
          if (typeof event.source !== "string" || typeof event.key !== "string") fail('event needs string "source" and "key"');
        }
        const d = gate.decide(event);
        console.log(JSON.stringify(d));
        process.exitCode = EXIT[d.action];
        return;
      }
      case "flush": {
        const rows = store.takePending();
        console.log(JSON.stringify(rows.map(brief), null, 2));
        return;
      }
      case "catchup": {
        console.log(JSON.stringify(catchup(store, parseSince(values.since), { includeFiltered: values["include-filtered"] }), null, 2));
        return;
      }
      case "rules": {
        console.log(JSON.stringify(rules(loadCfg(), store), null, 2));
        return;
      }
      case "mcp": {
        await serveStdio(loadCfg(), store);
        return;
      }
      case "log": {
        for (const r of store.recentDecisions(Number(values.limit))) {
          console.log(`${new Date(r.ts).toISOString()}  ${r.action.padEnd(5)}  ${r.source}/${r.key}  ${r.reason}`);
        }
        return;
      }
      default:
        fail(`unknown command "${cmd}"\n\n${HELP}`);
    }
  } finally {
    store.close();
  }
}

main().catch((err: unknown) => fail(err instanceof Error ? err.message : String(err)));
