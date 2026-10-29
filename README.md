# wakegate

A small, framework-agnostic gate that decides **whether an event should wake your AI agent**.

Always-on agents burn money and attention waking the LLM for events that need no action: heartbeats that return "nothing to do", the same webhook delivered twice, a bot comment, a burst of ten edits to one ticket. `wakegate` sits in front of the agent and answers one question per event: `wake`, `skip` or `batch`, with a recorded reason.

> Status: v0.2, early. Deterministic rules only. No learned model, no hosted service. Rewritten from TypeScript (tag `v0.1-typescript`) to Python; the benchmark outputs of the two versions were compared and are identical.

## Why not just a shell script?

A pre-wake script is a fine start, and many frameworks have one. wakegate adds the parts a script rarely gets right:

- **Per-key state.** The same payload for the same key is a duplicate; change detection and `catchup` fall out of it.
- **Budgets.** Daily wake cap, quiet hours, per-key cooldown. Over-budget events are batched, not lost.
- **Hard bypass and targeted exemptions.** `bypass` rules always wake. A normal rule can instead list `exempt: [cooldown, quietHours, budget]` to get past just those limits.
- **Batches are not stranded.** With `flushOnWake` (default), a wake also hands over everything batched earlier.
- **Audit log + replay.** Every decision, including skips, is stored with its reason. You can replay history against a new config before trusting it.
- **No code execution.** Rules are declarative data (`eq`, `in`, `matches`, ...), so a config can be reviewed instead of sandboxed.

## Quick start

Requires Python >= 3.11 and [uv](https://docs.astral.sh/uv/). Not on PyPI yet, so install from a checkout:

```bash
git clone https://github.com/yyydddkkk/wakegate.git && cd wakegate
uv tool install .            # puts `wakegate` on your PATH; use `uv tool install --editable .` while hacking on it

wakegate init                # writes wakegate.yaml
echo '{"source":"github","key":"pr/1","data":{"state":"open"}}' | wakegate check
echo $?   # 0 wake | 10 skip | 11 batch | 1 error
```

In a heartbeat script:

```bash
if wakegate check --event "$EVENT_JSON" >/dev/null; then run_agent; fi
```

Other commands: `report`, `review`, `label` (see *Measuring what the rules miss*), `rules` (active policy and today's standing), `poll <url>` (one conditional GET, then check), `flush` (print and clear batched events), `catchup --since 24h [--include-filtered]`, `log`, `replay <events.jsonl>`, `mcp` (see below).

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
uv run python examples/gen_heartbeat.py > examples/heartbeat.jsonl
uv run wakegate replay examples/heartbeat.jsonl --config examples/wakegate.yaml --cost-per-wake 1.40
```

```
events: 344
wake: 5   skip: 336   batch: 3
cost: 481.60 -> 7.00  (saved 474.60, 98.5%)
batched: 3 delivered with a later wake (no extra cost), 0 still waiting at the end of the log.
```

**Read this number carefully.** The input is generated: a week of 30-minute heartbeats (336 of the 344 events are noise by construction) plus 8 hand-written real events. The 98.5% describes that scenario, not any real workload. The `1.40` per wake is a placeholder taken from a self-reported figure in a public issue; replace it with yours.

## Stress test on synthetic traffic

`uv run python examples/bench_synthetic.py` replays 14 days of generated traffic (heartbeats, PRs, mail, pager alerts, CI) where every event has a ground-truth label, so misses can be counted. The numbers below are 5 seeds, about 71 must-wake events each, agent never calling `flush` by hand:

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

## Measuring what the rules miss

Step-by-step setup for trying this on your own traffic: [docs/shadow-mode.md](docs/shadow-mode.md).

A gate that drops events is only trustworthy if you can see what it dropped. Two tools, both off by default:

- **Shadow mode** (`mode: shadow` in the config, or `--shadow` on any command): every event is delivered as a wake, exactly as without wakegate. The verdict the rules *would* have given is recorded, and cooldown and budget are simulated from those verdicts. Try it on live traffic with nothing at risk, then read `wakegate report` to see how many wakes enforcing would have avoided.
- **Canary** (`canary: { rate: 0.02 }`): in enforce mode, that fraction of events the rules would have skipped (duplicates excluded) is delivered as a wake anyway. The decision carries `canary: true` and a note, and the receiving agent or person records `wakegate label <id> wanted|unwanted`. Canaries do not spend the daily budget.

`wakegate review --sample 30` draws a uniform random sample of suppressed events from the log for a person to label, which is the way to get a sample in shadow mode. `wakegate report` then estimates the share of suppressed events that were wanted, with a 95% Wilson interval, and breaks it down by rule so you can see which rule is hiding things. Only labels on canaries and on `review` samples enter the estimate; labels you add by hand are stored but not used for it.

Illustrative output, not a measurement:

```
miss estimate: 1 of 40 sampled suppressed events were wanted = 2.5% (95% CI 0.4% to 12.9%), roughly 1 to 34 missed events in this window
  rule ignore-bots: 1 of 6 sampled were wanted ...
```

**What this can and cannot tell you.** `examples/bench_canary.py` checks the estimator against synthetic traffic with known labels and a perfect reviewer: the intervals contain the true rate in about 90-100% of runs, so the estimate is honest. It is also *wide*: with a true miss rate of 1-2% among suppressed events, a 2% canary gives about 8 labels in two weeks and an interval about 36 points wide; 20% gives about 59 labels and about 8 points. Rare misses need hundreds of labels to see. Treat a canary as a tripwire ("a rule just hid something wanted: look at it") rather than a precise rate, and read the per-rule view, not only the overall number. The estimate assumes the reviewer is right; a tired reviewer or an agent labelling its own wakes will be noisier.

## MCP server

```bash
wakegate mcp --config wakegate.yaml --db .wakegate/state.db
```

```json
{ "mcpServers": { "wakegate": { "command": "wakegate", "args": ["mcp", "--config", "/path/to/wakegate.yaml", "--db", "/path/to/state.db"] } } }
```

| Tool | What it does | Read-only |
|---|---|---|
| `wakegate_check` | Submit an event, get `wake` / `skip` / `batch` and the reason | no (records the event) |
| `wakegate_catchup` | What changed since `24h` / an ISO date, plus how many events are batched. Changes your `skip` rules dropped are left out and only counted (`includeFiltered` shows them) | yes |
| `wakegate_rules` | The active rules, cooldown and budget, plus today's standing: quiet hours now, wakes used and left, batch size. Lets an agent explain a verdict | yes |
| `wakegate_flush` | Take the batched events once and clear them (not needed after a wake with `flushOnWake`) | no |
| `wakegate_log` | Recent decisions with ids, verdicts and reasons | yes |
| `wakegate_label` | Record `wanted` / `unwanted` for a decision id (canaries and review samples feed the miss estimate) | no |
| `wakegate_report` | What the rules decided, wakes shadow mode would have avoided, label counts, miss estimate with interval and per-rule breakdown | yes |

**What MCP can and cannot do here.** MCP is pull-based: the server cannot wake an agent that is not running. It is useful to an agent that is already awake (call `wakegate_catchup` / `wakegate_flush` to learn what happened while it was away) and to tools or other agents that want a verdict on an event. The thing that actually decides whether to start the agent is still the `check` exit code in your scheduler or heartbeat script. Both use the same database, so they agree.

The rules are fixed when the server starts; a client cannot change them. Over stdio the server trusts whoever launched it; there is no authentication layer.

## Library

```python
from wakegate import Gate, Store, WakeEvent, load_config

gate = Gate(load_config("wakegate.yaml"), Store(".wakegate/state.db"))
d = gate.decide(WakeEvent("github", "pr/1", data={"state": "open"}))
if d.action == "wake":
    run_agent()
```

## Not in this version

Stratified sampling (per-rule quotas), a labelling UI, adaptive polling schedules, a learned wake model, OAuth/credential handling, per-service connectors, a hosted service. The audit log is meant to become the training data for a learned layer later; that has not been built or validated.

## Known limits

- A duplicate is skipped even if it matches a bypass rule or an exempt rule; repeated identical critical alerts will not re-wake. Change the payload (for example a counter) if you want repeats.
- `poll` is a single request. Scheduling is yours (cron, a timer); respect the returned `pollIntervalHint`.
- A first-ever event for a key always counts as a change.

## Development

```bash
uv sync
uv run pytest
```

MIT
