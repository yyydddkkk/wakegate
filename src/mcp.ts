import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import { z } from "zod";
import type { Config } from "./config.ts";
import { Gate } from "./gate.ts";
import { brief, catchup, parseSince } from "./queries.ts";
import type { Store } from "./store.ts";

const json = (value: unknown) => ({ content: [{ type: "text" as const, text: JSON.stringify(value, null, 2) }] });
const failure = (message: string) => ({ isError: true, content: [{ type: "text" as const, text: message }] });

/**
 * MCP tools over the same gate and store the CLI uses.
 *
 * MCP is pull-based: this server cannot wake an agent that is not running.
 * What it gives a running agent is (a) `wakegate_catchup` / `wakegate_flush`
 * to learn what happened while it was away, and (b) `wakegate_check` so
 * tools and other agents can submit events and get a wake/skip/batch verdict.
 * The config is fixed when the server starts; clients cannot change rules.
 */
export function createServer(config: Config, store: Store): McpServer {
  const gate = new Gate(config, store);
  const server = new McpServer({ name: "wakegate", version: "0.1.0" });

  server.registerTool(
    "wakegate_check",
    {
      title: "Check an event",
      description:
        "Submit one event and get a decision: wake, skip or batch, with the reason. Records the event in the audit log and updates per-key state. " +
        "The same payload for the same (source, key) is reported as a duplicate.",
      inputSchema: {
        source: z.string().min(1).describe('Where the event came from, e.g. "github".'),
        key: z.string().min(1).describe('Stable identity of the thing that changed, e.g. "pr/412".'),
        data: z.any().optional().describe("Event payload. Hashed for change detection."),
        severity: z.string().optional().describe('Optional hint rules can match on, e.g. "critical".'),
        ts: z.number().int().optional().describe("Epoch milliseconds. Defaults to now."),
      },
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
    },
    (args) => {
      try {
        return json(gate.decide(args));
      } catch (err) {
        return failure(err instanceof Error ? err.message : String(err));
      }
    },
  );

  server.registerTool(
    "wakegate_catchup",
    {
      title: "What changed while I was away",
      description: "Per-key summary of content changes since a point in time, plus how many events wait in the batch. Read-only.",
      inputSchema: {
        since: z.string().default("24h").describe('A duration like "30m", "24h", "7d", or an ISO date.'),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    ({ since }) => {
      try {
        return json(catchup(store, parseSince(since)));
      } catch (err) {
        return failure(err instanceof Error ? err.message : String(err));
      }
    },
  );

  server.registerTool(
    "wakegate_flush",
    {
      title: "Take the batched events",
      description: "Return the events that were batched (cooldown, quiet hours, budget) and clear the batch. Each event is returned once.",
      inputSchema: {},
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
    },
    () => json(store.takePending().map(brief)),
  );

  server.registerTool(
    "wakegate_log",
    {
      title: "Audit log",
      description: "The most recent decisions with their reasons, oldest first. Read-only.",
      inputSchema: { limit: z.number().int().min(1).max(500).default(50) },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    ({ limit }) => json(store.recentDecisions(limit).map(brief)),
  );

  return server;
}

/** Serves over stdio until the client disconnects. Never write to stdout here: it is the protocol channel. */
export async function serveStdio(config: Config, store: Store, transport: Transport = new StdioServerTransport()): Promise<void> {
  const server = createServer(config, store);
  const closed = new Promise<void>((resolve) => {
    server.server.onclose = resolve;
  });
  await server.connect(transport);
  await closed;
}
