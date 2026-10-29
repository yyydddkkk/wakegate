# Trying wakegate in shadow mode

Shadow mode lets you see what wakegate *would* do on your real traffic without it ever holding anything back. Your agent wakes exactly as it does today. wakegate only records what its rules would have decided, and after a few days you can see how many wakes it would have saved and whether it would have hidden anything you cared about.

It takes about ten minutes to set up and needs a few minutes of labelling after that.

## What you need

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).
- Something that wakes your agent on events: a heartbeat script, a webhook handler, a cron job. You will add one line to it.
- wakegate is not on PyPI yet, so install it from a checkout:

```bash
git clone https://github.com/yyydddkkk/wakegate.git
cd wakegate && uv tool install .      # puts `wakegate` on your PATH
```

## 1. Write a first config

Pick a folder for your own config and data, and run everything from there:

```bash
mkdir ~/my-agent-gate && cd ~/my-agent-gate
wakegate init          # writes wakegate.yaml
```

The starter config is a reasonable guess, but **edit it to fit your traffic**. Read the comments in it. The two things worth getting right:

- `rules` are checked in order and the first match decides. A `skip` rule hides everything it matches, so put exceptions above it.
- `ignorePaths` lists fields that change on every event and mean nothing (timestamps, request ids). If you skip this, nothing ever looks like a duplicate and you will see almost no savings.

- Quiet hours and the daily budget use your machine's time zone unless you set `budget.timezone` (for example `Asia/Shanghai`). On a server that is probably UTC, which may not be what you mean.

You do not need to get the rules right now. Shadow mode is how you find out what is wrong with them.

## 2. Describe your events

Each time something would wake your agent, wakegate wants one small JSON object:

```json
{ "source": "github", "key": "pr/412", "severity": "critical", "data": { "state": "review_requested", "actor": "alice" } }
```

- `source`: where it came from (`github`, `mail`, `heartbeat`).
- `key`: the identity of the thing that changed (`pr/412`, `thread/88`). State is tracked per `(source, key)`, so this choice decides what counts as a duplicate. Use something stable. For a plain heartbeat, any constant such as `main` is fine.
- `data`: whatever distinguishes this event from the last one for the same key. Two events with the same `data` and key are duplicates.
- `severity` (optional): a hint your rules can match on.

**Privacy.** wakegate stores each event in a local SQLite file (`.wakegate/state.db`) so you can review it later. Put summaries and ids in `data`, not message bodies, tokens or personal data you would not want sitting in a file.

## 3. Add one line before your agent wakes

```bash
# in your heartbeat / webhook script, before you start the agent
wakegate check --shadow --event "$EVENT_JSON" >/dev/null || true
run_my_agent            # unchanged: it still runs every time
```

In shadow mode `check` always exits 0, which means "wake", so existing `if wakegate check; then ...` logic keeps working. The `|| true` makes sure a wakegate error can never stop your agent. If the event is built from stdin, `echo "$EVENT_JSON" | wakegate check --shadow`.

From an agent that has MCP, you can instead run `wakegate mcp --shadow` as an MCP server and have it call `wakegate_check`. Either way, only one place should feed events.

Run this where the config and `.wakegate/` folder from step 1 live, or pass `--config` and `--db` explicitly.

## 4. Check that it is recording

After a few events:

```bash
wakegate log --limit 10
```

Each line shows the verdict and the reason, and for shadow decisions `[shadow: woke, rules said skip]`. If you see nothing, your script is not reaching wakegate (wrong directory, `|| true` hiding an error: run the command by hand once).

## 5. Wait, then look

Give it a few days, long enough to cover your normal mix of traffic including a quiet stretch and a busy one.

```bash
wakegate report --since 7d --cost-per-wake 1.40
```

Use your own cost per wake. A rough guess is better than the placeholder, and you can leave the flag out.

What to read:

- **`rules decided`**: how the traffic splits into wake / skip / batch, and the top reasons. A very large `skip:duplicate` is the cheap win. A big `skip:rule:...` deserves a look.
- **`shadow: enforcing would have avoided N wakes`**: the saving you are shopping for. It counts events enforcing would have *dropped or delayed*. Delayed ones (batched by cooldown, quiet hours or budget) would still reach the agent with a later wake, so the number overstates what is gone for good. The line `rules decided` shows the split.
- **`suppressed by rules`**: events a rule (not a duplicate check) would have dropped. This is where a miss can hide, so this is what you label next.

## 6. Label a sample (this is the part that matters)

The saving is easy to count. Whether you would have lost something you cared about is not, and only you know.

```bash
wakegate review --sample 30 --since 7d
```

This draws 30 suppressed events **at random** and lists them with their data. For each one ask: *should this have woken the agent?*

```bash
wakegate label 17 unwanted          # fine to drop
wakegate label 42 wanted --note "real deploy failure"
```

Run `wakegate review` again to see what is left, and `wakegate report` for the result:

```
miss estimate: 2 of 30 sampled suppressed events were wanted = 6.7% (95% CI 1.8% to 21.3%) ...
  rule ignore-bots: 2 of 4 sampled were wanted ...
```

The rule breakdown is usually more useful than the overall number: it names the rule that is hiding things.

**How much to trust it.** Thirty labels give a wide interval. If none of them are wanted, you have learned the miss rate is probably small, not that it is zero: a miss rate of 1% needs hundreds of labels to see. Use the result as a tripwire. A single wanted event is a real finding even in a small sample. Only labels from `review --sample` (and canaries, see below) feed the estimate; labels you add to other events are stored but not used for it.

## 7. Decide

- **Wanted events found.** Look at the rule that suppressed them, fix or reorder it, add an `exempt` or a bypass rule, and keep running in shadow mode. Re-running a report after changing the config does not re-judge old events; the verdicts in the log are what the rules said at the time. Start a fresh `--db` to measure a new config from scratch.
- **Nothing wanted, saving looks worth it.** Drop `--shadow`. Add a small canary so you keep an eye on misses once real events are being held back:

  ```yaml
  canary: { rate: 0.05 }   # 5% of would-be skips still wake the agent, with a note asking you to label them
  ```

- **Saving is small.** Then there may be little to gain here. That is a result too.

## What shadow mode does not tell you

- It does not exercise batching: nothing is held back, so you will not see how long batched events would have waited or whether the agent would have collected them. Check `wakegate rules` and your cooldown and quiet-hours settings by reading them.
- It does not show how your agent behaves when it wakes less often.
- It measures only the traffic you fed it. Events that never reached wakegate are invisible.
- The estimate is only as good as your labels.

## If something looks wrong

- `wakegate log` shows every decision with its reason.
- `wakegate rules` prints the active policy and where today stands (quiet hours, wakes used).
- Bug reports and results, good or bad, are welcome at https://github.com/yyydddkkk/wakegate/issues.
