# wakegate

A small, framework-agnostic gate that decides **whether an event should wake your AI agent**.

Always-on agents burn money and attention waking the LLM for events that need no action: heartbeats that return "nothing to do", the same webhook delivered twice, a bot comment, a burst of ten edits to one ticket. `wakegate` sits in front of the agent and answers one question per event: `wake`, `skip` or `batch`, with a recorded reason.

> Status: v0.1, early. Deterministic rules only. No learned model, no hosted service.

## Why not just a shell script?

A pre-wake script is a fine start, and many frameworks have one. wakegate adds the parts a script rarely gets right:

- **Per-key state.** The same payload for the same key is a duplicate; change detection and `catchup` fall out of it.
- **Budgets.** Daily wake cap, quiet hours, per-key cooldown. Over-budget events are batched, not lost.
- **Hard bypass and targeted exemptions.** `bypass` rules always wake. A normal rule can instead list `exempt: [cooldown, quietHours, budget]` to get past just those limits.
- **Batches are not stranded.** With `flushOnWake` (default), a wake also hands over everything batched earlier.
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

Other commands: `rules` (active policy and today's standing), `poll <url>` (one conditional GET, then check), `flush` (print and clear batched events), `catchup --since 24h [--include-filtered]`, `log`, `replay <events.jsonl>`, `mcp` (see below).

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

A matching rule with `then: wake` can opt out of 4 and 5 with `exempt`. Rules are checked in order and the first match decides, so **a skip rule hides everything it matches**: put exceptions (a deploy bot failing, say) above it.

On a wake with `flushOnWake: true`, events batched earlier come back in the decision's `batch` field, once. If nothing wakes the agent for a long time, a batch can still sit there: watch `pendingBatch` in `catchup`.

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
batched: 3 delivered with a later wake (no extra cost), 0 still waiting at the end of the log.
```

**Read this number carefully.** The input is generated: a week of 30-minute heartbeats (336 of the 344 events are noise by construction) plus 8 hand-written real events. The 98.5% describes that scenario, not any real workload. The `1.40` per wake is a placeholder taken from a self-reported figure in a public issue; replace it with yours. 
## Stress test on synthetic traffic

`node examples/bench-synthetic.ts` replays 14 days of generated traffic (heartbeats, PRs, mail, pager alerts, CI) where every event has a ground-truth label, so misses can be counted. The numbers below are 5 seeds, about 71 must-wake events each, agent never calling `flush` by hand:

| Config | Must-wake delivered instantly | ...eventually | Nice-to-know eventually |
|---|---|---|---|
| wake on everything | 100% | 100% | 100% |
| first version, shipped example | 37% | 37% | 21% |
| now, shipped example | 84% | 99% | 98% |
| first version, tuned rules + cooldown + quiet hours + budget | 71% | 71% | 44% |
| now, same tuned rules | 96% | 99.5% | 99% |

What this shows, and what it does not:

- The first run found real defects: a cooldown that delayed an important change on the same item, a budget that could not tell important from unimportant, a bot rule that swallowed a failing deploy bot, and batches that were never delivered. `exempt`, `flushOnWake` and the reordered example fix those.
- It is the author's traffic, labels and traps. The first version of the test scored 100% because it had no trap for these defects. The example config's rules were then written with the benchmark's traps in mind, so part of the "now" improvement is rules fitted to the test, not proof the defaults generalise. The improvements that do not depend on that are `flushOnWake` (a batch used to be lost) and `exempt` (a mechanism, not a rule).
- Waking costs a little more after the fix (about 9% more wakes on the tuned config), because important changes now get through.
- Still lossy by design: `overBudget: skip` drops events for good (the "nice-to-know" column falls to about 80%), and quiet hours still delay a late-evening review request until the next wake.
- Do not read any of this as a real-world miss rate.

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
| `wakegate_catchup` | What changed since `24h` / an ISO date, plus how many events are batched. Changes your `skip` rules dropped are left out and only counted (`includeFiltered` shows them) | yes |
| `wakegate_rules` | The active rules, cooldown and budget, plus today's standing: quiet hours now, wakes used and left, batch size. Lets an agent explain a verdict | yes |
| `wakegate_flush` | Take the batched events once and clear them (not needed after a wake with `flushOnWake`) | no |
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

- A duplicate is skipped even if it matches a bypass rule or an exempt rule; repeated identical critical alerts will not re-wake. Change the payload (for example a counter) if you want repeats.
- `poll` is a single request. Scheduling is yours (cron, a timer); respect the returned `pollIntervalHint`.
- A first-ever event for a key always counts as a change.

## Development

```bash
npm test          # node:test, no extra dependencies
npm run typecheck
npm run build     # compiles to dist/
```

MIT
