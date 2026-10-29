# Layered filtering and the decision trace

Status: step 1 of the plan is implemented (pipeline, built-in layers, trace recording, `build_trace`, JSON Schema). Steps 2 onward are not. Written against v0.2 (Python).

Decisions taken since the first draft:

- Layers that did not run are **absent** from the trace (no `not_run` rows). Funnel counts are computed from what ran.
- Governors (cooldown, quiet hours, budget) **are layers**, three separate ones, each present only when configured.
- Artifact defaults (not kept unless asked, 7 days, 16 KB) stay as proposed for now.
- A layer that raises **fails open** (`wake`).
- **Screenshots and attachments are out of scope for now.** The sections that mention them are kept only to show the design does not block them.

## Why

Today `Gate.decide()` is one function that mixes four different jobs (duplicate check, bypass, rules, limits) and records one verdict and one reason. Two things we want next do not fit that shape:

1. **More layers.** A cheap classifier, later an LLM triage step, later image layers for screenshots.
2. **A way for the model (or a person) to see what each layer saw and decided**, without that becoming the default path that defeats the filter.

Both need the same thing: a **record of every layer's input, output and cost for every decision**. That record is the *trace*. The cascade is only worth building if each layer can be measured on its own, and the trace is what makes that possible.

Evidence from earlier research that shapes the design: learned filters were mediocre in published results (AUC around 0.64 to 0.74, a cheap-model website monitor filtered correctly about 45% of the time in one write-up, classifiers drift). So a learned layer must prove itself with data before it is allowed to drop anything. The shadow and canary machinery we already have is how it proves itself.

## Non-goals

- No learned layer is built here. This document defines the slot and the record, not the model.
- No screenshot processing yet. The trace must not block it (see *Attachments*).
- No change to behaviour of the four existing layers. Step 1 is a refactor that must leave every benchmark output byte-identical.

## Concepts

### Layer

A layer takes the event plus what earlier layers produced and returns a **result**. Four kinds:

| Kind | Job | Example | Cost |
|---|---|---|---|
| `detector` | derives facts, may end the cascade | dedupe (fingerprint compare) | free |
| `rules` | declarative decision | bypass rules, the rules list | free |
| `classifier` | estimates relevance | embedding similarity, small model, LLM triage | cheap to expensive |
| `governor` | applies limits to a proposed wake | cooldown, quiet hours, daily budget | free |

The existing pipeline maps to: `dedupe` (detector), `bypass` (rules), `rules` (rules), `limits` (governor). New classifiers slot in between `rules` and `limits`, because relevance should be decided before limits are applied.

### Result

```
outcome     skip | wake | batch | defer | error
final       true  = nothing later may change this outcome
            false = a proposal; governors may still turn a wake into batch/skip
reason      machine-readable ("duplicate", "rule:ignore-bots", "cooldown", ...)
rule        name of the rule, if any
confidence  0..1 or null (classifiers only)
views       named derived data later layers may read (see below)
detail      layer-specific explanation, small and JSON
cost        { ms, tokens_in, tokens_out, usd }  (nulls where not applicable)
```

`defer` means "I do not have a confident answer, ask the next layer". It is the only honest output for a learned layer that is unsure.

How today's behaviour is expressed:

- dedupe skip: `skip, final=true`.
- bypass match: `wake, final=true` (limits do not apply).
- a rule with `then: skip` or `batch`: that outcome, `final=true`.
- a rule with `then: wake`: `wake, final=false`. Its `exempt` list is carried in `detail.exempt`, and each governor honours it.
- no rule matches: the configured default, `final=false` if it is wake.
- governors run only when the current proposal is a non-final wake, and may turn it into `batch` or `skip` (`final=true`).

### Per-layer mode

`shadow` and `canary` are currently global. They should be per layer:

| Layer mode | Meaning |
|---|---|
| `off` | not run |
| `enforce` | outcome is applied |
| `shadow` | runs and is recorded, outcome is **not** applied (`applied=false`) |
| `canary` | enforced, but a sampled fraction of its skips are released as wakes |

The global `mode: shadow` stays as shorthand for "every layer shadow". The point of the change: a new classifier can run in `shadow` while the rules stay in `enforce`, and its would-be mistakes are measured on real traffic before it is trusted. Canary release is attributed to the layer that skipped the event, so miss estimates become **per layer**, not just per rule.

### Views

A layer may publish **views**: named derived data (`masked_data`, `ocr_text`, `phash`, `summary`). A later layer declares which view it consumes (default: the raw event). This is what lets a chain such as image, perceptual hash, OCR text, small model, LLM work without every layer re-deriving things, and it is what the model later gets to look at.

### Failure policy

Every layer has `on_error` (`wake` by default) and a timeout. A layer that crashes or times out records `outcome=error` and the cascade **fails open**: the event is treated as a wake proposal and continues. A broken filter should cost money, not silently drop events. Only `rules` and `detector` layers are allowed `on_error: skip`, and doing so should be an explicit config choice.

## The trace

One trace per decision. It is plain JSON so any language can read it; version it with `trace_version`.

```json
{
  "trace_version": 1,
  "decision_id": 4812,
  "event": { "source": "github", "key": "pr/412", "ts": 1767614400000, "digest": "sha256:ab12..." },
  "layers": [
    {
      "seq": 0, "layer": "dedupe", "kind": "detector", "mode": "enforce", "applied": true,
      "outcome": "defer", "final": false, "reason": "changed",
      "detail": { "fingerprint": "1ebcd8...", "previous": "9f03aa..." },
      "views": [],
      "cost": { "ms": 0.2, "tokens_in": null, "tokens_out": null, "usd": null }
    },
    {
      "seq": 1, "layer": "rules", "kind": "rules", "mode": "enforce", "applied": true,
      "outcome": "skip", "final": true, "reason": "rule:ignore-bots", "rule": "ignore-bots",
      "detail": { "evaluated": 3, "matched_index": 1 },
      "views": [],
      "cost": { "ms": 0.1, "tokens_in": null, "tokens_out": null, "usd": null }
    }
  ],
  "result": { "verdict": "skip", "decided_by": "rules", "delivered": "skip", "mode": "enforce" }
}
```

Notes:

- The cascade **stops at the first final outcome**. Layers after it are simply absent (or `{"outcome": "not_run"}` if a record of the omission is wanted; to be decided, see open questions).
- `result.verdict` is what the *applied* layers decided. `delivered` is what the caller was told; they differ for canaries and global shadow, exactly as `verdict` and `action` do today.
- A layer in `shadow` appears with `applied=false`. It never changes `verdict`.
- `event.digest` identifies the raw event. The event itself is stored once, not copied into every trace.

### Per-layer detail, by kind

- `dedupe`: `fingerprint`, `previous`, `changed`.
- `rules` / `bypass`: `evaluated` (how many rules were tried), `matched_index`, `exempt`. Optionally the list of `{name, matched}` for up to 50 rules, switched on in config, since it is what makes "why not rule X?" answerable.
- `limits`: `checks: [{name: "cooldown"|"quiet_hours"|"budget", applied, exempt, value}]`, e.g. `value: {used: 10, max: 10}` for the budget.
- `classifier`: `model`, `score`, `threshold`, `label`.

## Storage

Additive and backwards compatible. Old rows have no trace; a minimal one can be synthesised from `reason` when asked.

```sql
CREATE TABLE layer_runs (            -- one row per layer per decision; makes the funnel queryable
  decision_id INTEGER NOT NULL, seq INTEGER NOT NULL,
  layer TEXT NOT NULL, kind TEXT NOT NULL, mode TEXT NOT NULL, applied INTEGER NOT NULL,
  outcome TEXT NOT NULL, final INTEGER NOT NULL, reason TEXT, rule TEXT, confidence REAL,
  duration_ms REAL, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,
  detail_json TEXT, PRIMARY KEY (decision_id, seq)
);

CREATE TABLE artifacts (             -- derived views worth keeping; bounded and expiring
  id INTEGER PRIMARY KEY, decision_id INTEGER NOT NULL, layer TEXT NOT NULL, name TEXT NOT NULL,
  mime TEXT NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
  content BLOB, path TEXT,            -- inline for small text, a path for images
  untrusted INTEGER NOT NULL DEFAULT 1, expires_ts INTEGER
);

CREATE TABLE access_log (            -- every time the trace or a raw view is shown to a model or person
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, actor TEXT NOT NULL,
  kind TEXT NOT NULL,                 -- inspect | peek
  decision_id INTEGER, layer TEXT
);
```

Defaults: artifacts are not kept unless a layer asks for it or config sets `keep_views: [name]`; kept artifacts expire after 7 days; text artifacts are capped at 16 KB, anything bigger keeps only its digest. These are guesses to be tuned.

## The bypass

"Let the model see each layer's raw information" is two different features, and they carry different risk.

### Inspect (read the trace)

`inspect(decision_id, layer=None)` returns the trace, and for a named layer the **input that layer saw** (the raw event or the view it consumed). Read-only, logged in `access_log`.

### Peek (ask for suppressed events)

`peek(layer, outcome, since, limit)` lists events a layer suppressed, so an agent can decide for itself whether something was wrongly dropped. Useful, and risky in three ways:

1. **Prompt injection.** Raw external content is exactly what a filter exists to keep away from a model. Everything returned is wrapped as untrusted (`{"untrusted": true, "source": ..., "content": ...}`), truncated to a size cap, and passed through configurable redaction patterns first. Tool descriptions say that the content is data, not instructions. This reduces the risk; it does not remove it.
2. **Defeating the filter.** If peek is the normal path, the filter saves nothing. A daily peek budget (config, default small) and an `access_log` keep that visible, and the report shows the peek count next to the savings.
3. **Cost of reading.** Large payloads are cut off; the trace says how many bytes were omitted.

A peek is also the best label we can get: an agent that peeks at a suppressed event and says "this mattered" has produced a `wanted` label with context. Peek therefore ends by suggesting `wakegate_label`, and labels given right after a peek are linked to it. They are **not** counted as random samples for the miss estimate, because the agent chose what to look at; they feed a separate "found by inspection" count.

## Reports

The funnel is the headline number for a cascade:

```
layer      entered  resolved  deferred  skipped  cost(ms)  cost(usd)
dedupe        5200       310      4890      310       1.1       0
rules         4890      3960       930     3960       0.9       0
classifier     930       610       320      610     2300     0.012
limits         320        40       280       40       0.1       0
-> wakes 280 of 5200 events (5.4%)
```

and the miss estimate becomes per layer and rule: "rules/ignore-bots: 1 of 6 sampled were wanted", "classifier: 4 of 31 sampled were wanted". A layer whose interval is wide or whose point estimate is high stays in `shadow`.

## Compatibility

- Existing YAML keeps working: it is read as the default layer list `[dedupe, bypass, rules, limits]`. A new optional `layers:` section adds or reorders layers and sets per-layer `mode`, `on_error` and `timeout`.
- `Decision` and the CLI/MCP JSON keep their current keys. `trace` is added only to `inspect`, `log --trace` and an opt-in `wakegate_check` flag; it is off by default so responses stay small.
- Python is the reference implementation. The JSON shapes above, not the Python classes, are the contract, so other languages can read and write traces. A JSON Schema file is produced in step 1.

## Plan

Each step ships on its own and is checked against the previous behaviour.

1. **Pipeline refactor + trace recording.** *Done.* `Gate.decide()` now runs `dedupe, bypass, rules, cooldown, quiet_hours, budget` through `pipeline.run_pipeline` and writes `layer_runs`. Behaviour is unchanged: `tests/test_golden.py` compares a hash of every decision (11 configs x 2 seeds, including canary and shadow), the stress test, the canary benchmark and the replay output against fixtures generated before the refactor. A decision costs about 30 microseconds more (0.036 to 0.066 ms in memory), which does not matter at event rates.
2. **Inspect and funnel.** `wakegate inspect`, `log --trace`, `wakegate_inspect` MCP tool, funnel in `report`.
3. **Per-layer modes.** Shadow and canary per layer, layer-attributed miss estimates.
4. **Layer interface for new layers**, plus one deliberately simple learned layer (embedding similarity to previously wanted events) running in `shadow` against real labels. If it does not beat the rules in shadow, that is the answer.
5. **Peek.** With redaction, budgets and the access log.
6. **Attachments / screenshots** (deferred; not planned yet). Perceptual hash and mask-aware pixel diff first (free, deterministic), then OCR, then a vision model only on what survives.

Steps 1 to 3 are small and certain to be useful. Steps 4 to 6 should wait for evidence that they are needed.

## Attachments (so the trace does not block screenshots)

The event envelope gets an optional `attachments: [{id, mime, bytes, sha256, ref}]`, where `ref` points at the stored file, not the bytes. Image layers read an attachment and publish views (`phash`, `ocr_text`, `thumbnail`). Raw images are never copied into the trace; a thumbnail artifact is kept only if configured, with the same expiry as other artifacts. Masks (regions to ignore, such as clocks and ads) are the image equivalent of `ignorePaths` and live in layer config.

## Open questions

1. **Per-layer mode vs. global mode.** Keep both (global as shorthand for every layer in shadow)? Leaning yes. Step 3.
2. **Redaction.** Regex patterns in config are cheap but leaky. Is that acceptable for v1 of peek? Step 5.
3. **Layer configuration.** Today the layer list is derived from the config and cannot be reordered from YAML. A `layers:` section (reorder, per-layer mode, `on_error`, timeout) comes with step 3 or 4, when there is a second kind of layer to configure.
4. **Timeouts.** `on_error` exists on every layer; timeouts are not enforced yet because no built-in layer can be slow.
