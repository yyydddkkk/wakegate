import assert from "node:assert/strict";
import { test } from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { parseConfig } from "../src/config.ts";
import { createServer } from "../src/mcp.ts";
import { Store } from "../src/store.ts";

async function connect(yaml = "version: 1") {
  const store = new Store(":memory:");
  const server = createServer(parseConfig(yaml), store);
  const [a, b] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: "test", version: "0" });
  await Promise.all([server.connect(a), client.connect(b)]);
  const call = async (name: string, args: Record<string, unknown> = {}) => {
    const res = await client.callTool({ name, arguments: args });
    const text = (res.content as Array<{ text: string }>)[0]!.text;
    return { isError: Boolean(res.isError), text, json: res.isError ? undefined : JSON.parse(text) };
  };
  return { client, call, store };
}

test("exposes the five tools, read-only ones annotated", async () => {
  const { client } = await connect();
  const { tools } = await client.listTools();
  assert.deepEqual(tools.map((t) => t.name).sort(), ["wakegate_catchup", "wakegate_check", "wakegate_flush", "wakegate_log", "wakegate_rules"]);
  const ro = Object.fromEntries(tools.map((t) => [t.name, t.annotations?.readOnlyHint]));
  assert.equal(ro.wakegate_catchup, true);
  assert.equal(ro.wakegate_log, true);
  assert.equal(ro.wakegate_rules, true);
  assert.equal(ro.wakegate_check, false);
});

test("check returns a decision and the second identical call is a duplicate", async () => {
  const { call } = await connect();
  const first = await call("wakegate_check", { source: "github", key: "pr/1", data: { state: "open" } });
  assert.equal(first.json.action, "wake");
  const second = await call("wakegate_check", { source: "github", key: "pr/1", data: { state: "open" } });
  assert.equal(second.json.reason, "duplicate");
});

test("invalid input is rejected by the schema, not the gate", async () => {
  const { call } = await connect();
  const res = await call("wakegate_check", { source: "", key: "k" });
  assert.equal(res.isError, true);
});

test("catchup, flush and log reflect what check recorded", async () => {
  const { call } = await connect("flushOnWake: false\nrules: [{ name: later, when: { path: source, eq: mail }, then: batch }]");
  await call("wakegate_check", { source: "mail", key: "t/1", data: 1 });
  await call("wakegate_check", { source: "gh", key: "pr/2", data: 1 });

  const cu = await call("wakegate_catchup", { since: "1h" });
  assert.equal(cu.json.pendingBatch, 1);
  assert.equal(cu.json.changed.length, 2);

  const flushed = await call("wakegate_flush");
  assert.equal(flushed.json.length, 1);
  assert.equal(flushed.json[0].key, "t/1");
  assert.equal((await call("wakegate_flush")).json.length, 0);

  const log = await call("wakegate_log", { limit: 10 });
  assert.deepEqual(log.json.map((r: { action: string }) => r.action), ["batch", "wake"]);
});

test("a bad since value is a tool error, not a crash", async () => {
  const { call } = await connect();
  const res = await call("wakegate_catchup", { since: "yesterday-ish" });
  assert.equal(res.isError, true);
  assert.match(res.text, /duration/);
});

test("catchup hides changes dropped by skip rules, counts them, and can include them", async () => {
  const { call } = await connect(`
rules:
  - { name: ignore-bots, when: { path: data.actor, matches: "\\\\[bot\\\\]$" }, then: skip }
`);
  await call("wakegate_check", { source: "gh", key: "pr/1", data: { actor: "alice" } });
  await call("wakegate_check", { source: "gh", key: "pr/2", data: { actor: "ci[bot]" } });

  const quiet = await call("wakegate_catchup", { since: "1h" });
  assert.deepEqual(quiet.json.changed.map((c: { key: string }) => c.key), ["pr/1"]);
  assert.equal(quiet.json.filteredBySkipRules, 1);

  const all = await call("wakegate_catchup", { since: "1h", includeFiltered: true });
  assert.deepEqual(all.json.changed.map((c: { key: string }) => c.key).sort(), ["pr/1", "pr/2"]);
  assert.equal(all.json.filteredBySkipRules, 0);
});

test("rules shows the active policy and today's standing", async () => {
  const { call } = await connect(`
bypass: [{ name: critical, when: { path: severity, eq: critical } }]
rules: [{ name: noise, when: { path: source, eq: hb }, then: skip }]
budget: { maxWakesPerDay: 3, timezone: UTC }
`);
  await call("wakegate_check", { source: "gh", key: "a", data: 1 });
  const r = (await call("wakegate_rules")).json;
  assert.equal(r.bypass[0].name, "critical");
  assert.deepEqual(r.bypass[0].when, { path: "severity", eq: "critical" });
  assert.equal(r.rules[0].then, "skip");
  assert.equal(r.budget.maxWakesPerDay, 3);
  assert.equal(r.now.wakesToday, 1);
  assert.equal(r.now.wakesRemainingToday, 2);
  assert.equal(typeof r.now.inQuietHours, "boolean");
});

test("check description tells the agent what batch means and where to go next", async () => {
  const { client } = await connect();
  const check = (await client.listTools()).tools.find((t) => t.name === "wakegate_check")!;
  assert.match(check.description!, /wakegate_flush/);
  assert.match(check.description!, /wakegate_rules/);
});

test("a wake over MCP carries the earlier batch, so flush is not needed", async () => {
  const { call } = await connect("rules: [{ name: later, when: { path: source, eq: mail }, then: batch }]");
  await call("wakegate_check", { source: "mail", key: "t/1", data: 1 });
  const wake = await call("wakegate_check", { source: "gh", key: "pr/1", data: 1 });
  assert.equal(wake.json.action, "wake");
  assert.deepEqual(wake.json.batch.map((b: { key: string }) => b.key), ["t/1"]);
  assert.equal((await call("wakegate_flush")).json.length, 0);
});
