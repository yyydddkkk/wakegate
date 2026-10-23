# wakegate

A small, framework-agnostic gate that decides **whether an event should wake your AI agent**.

Always-on agents burn money and attention waking the LLM for events that need no action: heartbeats that return "nothing to do", the same webhook delivered twice, a bot comment, a burst of ten edits to one ticket. `wakegate` sits in front of the agent and answers one question per event: `wake`, `skip` or `batch`, with a recorded reason.

> Status: v0.1, early. Deterministic rules only. No learned model, no hosted service.

## Why not just a shell script?

A pre-wake script is a fine start, and many frameworks have one. wakegate adds the parts a script rarely gets right:

- **Per-key state.** The same payload for the same key is a duplicate; change detection and `catchup` fall out of it.
- **Budgets.** Daily wake cap, quiet hours, per-key cooldown. Over-budget events are batched, not lost.
- **Hard bypass.** Rules marked `bypass` always wake, so a budget can never swallow a critical alert.
- **Audit log + replay.** Every decision, including skips, is stored with its reason. You can replay history against a new config before trusting it.
- **No code execution.** Rules are declarative data (`eq`, `in`, `matches`, ...), so a config can be reviewed instead of sandboxed.

## Quick start

Requires Node >= 22.18 (runs TypeScript directly and uses the built-in `node:sqlite`).

```bash
npm install
npm run wakegate -- init                       # writes wakegate.yaml
echo '{"source":"github","key":"pr/1","data":{"state":"open"}}' | npm run -s wakegate -- check
echo $?   # 0 wake | 10 skip | 11 batch | 1 error
```

In a heartbeat script:

```bash
if wakegate check --event "$EVENT_JSON" >/dev/null; then run_agent; fi
```

Other commands: `poll <url>` (one conditional GET, then check), `flush` (print and clear batched events), `catchup --since 24h`, `log`, `replay <events.jsonl>`, `mcp` (see below).

## Event envelope

```json
{ "source": "github", "key": "pr/412", "ts": 1767000000000, "severity": "critical", "data": { "state": "review_requested" } }
```

State is tracked per `(source, key)`. `data` is hashed for change detection; `ignorePaths` removes fields that change every time but mean nothing.

## Decision order

1. **duplicate**: same fingerprint as last time for this key -> `skip`
2. **bypass** rule matches -> `wake` (skips 4 and 5)
3. first matching **rule**, else `default`
4. **cooldown**: woke for this key recently -> `batch`
5. **quiet hours** -> `batch`; **daily budget** exceeded -> `batch` or `skip`

See [`examples/wakegate.yaml`](examples/wakegate.yaml).

## Replay demo (synthetic)

```bash
node examples/gen-heartbeat.ts > examples/heartbeat.jsonl
node src/cli.ts replay examples/heartbeat.jsonl --config examples/wakegate.yaml --cost-per-wake 1.40
```

```
events: 344
wake: 5   skip: 336   batch: 3
cost: 481.60 -> 7.00  (saved 474.60, 98.5%)
```

**Read this number carefully.** The input is generated: a week of 30-minute heartbeats (336 of the 344 events are noise by construction) plus 8 hand-written real events. The 98.5% describes that scenario, not any real workload. The `1.40` per wake is a placeholder taken from a self-reported figure in a public issue; replace it with yours. Batched events are not counted, and each flush that wakes the agent adds one wake.

## MCP server

```bash
wakegate mcp --config wakegate.yaml --db .wakegate/state.db
```

```json
{ "mcpServers": { "wakegate": { "command": "npx", "args": ["wakegate", "mcp", "--config", "/path/to/wakegate.yaml", "--db", "/path/to/state.db"] } } }
```

| Tool | What it does | Read-only |
|---|---|---|
| `wakegate_check` | Submit an event, get `wake` / `skip` / `batch` and the reason | no (records the event) |
| `wakegate_catchup` | What changed since `24h` / an ISO date, plus how many events are batched | yes |
| `wakegate_flush` | Take the batched events once and clear them | no |
| `wakegate_log` | Recent decisions with reasons | yes |

**What MCP can and cannot do here.** MCP is pull-based: the server cannot wake an agent that is not running. It is useful to an agent that is already awake (call `wakegate_catchup` / `wakegate_flush` to learn what happened while it was away) and to tools or other agents that want a verdict on an event. The thing that actually decides whether to start the agent is still the `check` exit code in your scheduler or heartbeat script. Both use the same database, so they agree.

The rules are fixed when the server starts; a client cannot change them. Over stdio the server trusts whoever launched it; there is no authentication layer.

## Library

```ts
import { Gate, Store, loadConfig } from "wakegate";

const gate = new Gate(loadConfig("wakegate.yaml"), new Store(".wakegate/state.db"));
const d = gate.decide({ source: "github", key: "pr/1", data: { state: "open" } });
if (d.action === "wake") await runAgent();
```

## Not in v0.1

Adaptive polling schedules, a learned wake model, OAuth/credential handling, per-service connectors, a hosted service. The audit log is meant to become the training data for a learned layer later; that has not been built or validated.

## Known limits

- A duplicate is skipped even if it matches a bypass rule; repeated identical critical alerts will not re-wake. Change the payload (for example a counter) if you want repeats.
- `poll` is a single request. Scheduling is yours (cron, a timer); respect the returned `pollIntervalHint`.
- A first-ever event for a key always counts as a change.

## Development

```bash
npm test          # node:test, no extra dependencies
npm run typecheck
npm run build     # compiles to dist/
```

MIT
