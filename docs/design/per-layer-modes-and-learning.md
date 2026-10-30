# Per-layer modes, a plugin interface, and a first learned layer

Status: steps 3 and 4 of [layers-and-trace.md](layers-and-trace.md). **Steps 3a and 3b are implemented** (layer modes in the pipeline, the `layers:` section with validation, the in-process type registry, shadow layers in the funnel; per-layer canary rates, estimates stratified by layer, `review --layer`). Everything else here is still proposal. Written against the code at `6c9bb07`.

Decisions taken on the first draft:

- A shadow layer's counterfactual is **marginal**: it does not model its effect on cooldown and budget state. Reports say so.
- Classifiers **may only skip or defer** in this version. The pipeline treats a classifier that returns `wake` or `batch` as broken and fails open.
- wakegate **will ship a wrapper**, `wakegate run -- <command>`, that wakes, runs the agent command and reports `acted` or `noop` (with a `--noop-match` pattern), to solve "who reports outcomes". It comes with the outcomes step.

Implementation notes for 3a: the registry is in-process (`register_layer_type`); entry-point discovery arrives with 4a. Because PyYAML follows YAML 1.1, configs are now parsed with a YAML 1.2 core-schema loader, so `mode: off`, `eq: on` and an unquoted `22:00` mean what they look like.

## What problem this solves

Steps 1 and 2 made every layer visible. They did not make it safe to add a layer that can be wrong. A rule is wrong in a way you can read; a learned layer is wrong in a way you can only measure. So before building one we need:

1. **A way to run a layer without letting it act** (shadow), and to let it act while still sampling its mistakes (canary), **per layer** rather than for the whole gate.
2. **A way to add a layer** without editing the gate.
3. **A source of ground truth that does not depend on a person labelling things**, because nobody will label hundreds of events. This is the part most likely to decide whether a learned layer is worth having.
4. **One deliberately simple learned layer, held to a baseline it must beat**, so "the model helped" is a measurement and not a feeling.

Evidence that sets the bar: published learned triggers had mediocre accuracy (AUC about 0.64 to 0.74), a cheap-model website monitor was right about 45% of the time in one write-up, and classifiers drift. The rule baselines were close behind. Expect a learned layer to add little unless there is a messy middle that rules cannot describe. The design should make that discoverable cheaply, and make "drop the idea" an acceptable result.

## Non-goals

- No embeddings, no external model calls, no LLM triage in this step. The interface must allow them; the first learned layer must not need them.
- No automatic promotion from shadow to enforce. A report says whether the numbers justify it; a person changes the config.
- No timeouts for layers yet (see open questions).
- Screenshots stay out of scope.

---

# Part A. Per-layer modes (step 3)

## Modes

| Mode | Runs | Recorded | Affects the cascade | Affects delivery |
|---|---|---|---|---|
| `off` | no | no | no | no |
| `shadow` | yes | yes (`applied=false`) | **no, as if it had deferred** | no |
| `enforce` | yes | yes | yes | yes |
| `enforce` + canary | yes | yes | yes | a sampled fraction of its skips are released as wakes |

The existing global `mode: shadow` stays as it is: it overrides *delivery* (every event wakes) so you can try the whole gate risk-free. Per-layer `shadow` is different: delivery follows the other layers, and only this layer is held back. Both can be used; they answer different questions.

### What a shadow layer does to the cascade

It runs, its result is written to the trace with `applied=false`, and the pipeline then proceeds **as if the layer had returned `defer`**. In particular a shadow layer that says `skip` does not end the cascade; later layers still run. This is what makes the comparison fair: the enforced cascade is exactly what it would have been without the layer.

Consequence worth stating: a shadow layer's counterfactual is *marginal*. It answers "of the events that the enforced cascade would have woken the agent for, which would this layer have skipped?" It does not simulate second-order effects (if it had skipped event A, cooldown and budget state would have differed for event B). With a layer that sits just before the governors, that error is small; it is noted in reports rather than modelled.

A shadow layer that raises is recorded as `error` and ignored. It never fails open into a wake, because it had no say to begin with.

## Configuration

A new optional `layers:` section orders the pipeline and sets modes. Without it nothing changes: the layer list is derived from the existing keys, exactly as today (the golden tests in `tests/test_golden.py` enforce this).

```yaml
layers:
  - dedupe
  - bypass
  - rules
  - name: noop-bayes          # a layer instance
    type: bayes               # a registered layer type (Part B)
    mode: shadow
    params: { min_examples: 40, skip_below: 0.02 }
  - cooldown
  - quiet_hours
  - budget

canary:
  rate: 0.02                  # unchanged: applies to every enforced layer that can skip
  layers:                     # optional per-layer rates override it
    noop-bayes: 0.10
```

Validation:

- Built-in names refer to built-ins. Listing a layer whose settings are missing (for example `cooldown` with no `cooldownSeconds`) is an error, not a silent no-op.
- Governors must come after every layer that can propose a wake. A classifier placed after a governor is an error.
- Unknown `type` is an error that lists the registered types.
- `mode: enforce` on a learned layer **requires** a canary rate above zero for that layer (default floor 2%). Reason: once a layer skips events, nobody ever learns whether they mattered; the training signal for exactly those events disappears. A canary is what keeps the feedback loop alive. This is validated, not just recommended.

## Canary attribution

The canary already releases a sampled fraction of skipped events. It becomes layer-aware:

- Candidates are events whose **deciding layer** (`decision_meta.decided_by`) is an enforced layer with a canary rate, excluding `dedupe` duplicates as today.
- The released decision records which layer it overrode (the layer's trace row already becomes `mode=canary, applied=false`).
- Samples therefore estimate a miss rate **per layer**, not only per rule: "rules/ignore-bots: 1 of 6 wanted", "noop-bayes: 3 of 40 wanted".

## Evaluating a shadow layer

Two kinds of ground truth, in order of preference:

1. **Outcomes** (Part C): free, automatic, available for every event that woke the agent. A shadow layer's would-be skips are, by construction, events that *did* wake the agent in the enforced cascade, so an outcome exists for them.
2. **Labels**: human or agent judgement, used when outcomes are missing or doubtful.

`wakegate report` gains a per-layer section for shadow layers:

```
noop-bayes (shadow)   trained on 212 outcomes
  would have skipped 140 of 410 wakes (34%)
  of those, with an outcome: 131
     noop (correctly skipped): 124      acted (would have lost): 7
     false-skip rate 5.3%   95% CI 2.6% to 10.6%
  base rate of `acted` among all wakes: 19%
  promotion check: NOT ready (upper bound 10.6% > 5% target; needs more outcomes)
```

The promotion check is advisory and uses two config numbers (`max_false_skip`, `min_outcomes`, defaults 5% and 100, guesses). It never changes a mode.

**Evaluation is prequential.** The layer predicts at decision time using only what it knew then; the outcome arrives later; nothing is scored against a model that had already seen it. The prediction (`confidence`) is already in the trace, so scoring is a join, with no leakage to engineer around.

---

# Part B. Layer plugin interface (step 4a)

## Contract

`wakegate.layers.Layer` already exists. It gains what a layer that needs configuration and memory requires:

```python
class Layer(ABC):
    api_version = 1
    name: str                    # instance name, unique in the pipeline
    kind: LayerKind              # "classifier" for new layers
    on_error: Action = "wake"
    params_schema: dict | None   # JSON Schema for `params`, validated at config load

    @classmethod
    def create(cls, name: str, params: dict, services: LayerServices) -> "Layer": ...
    def run(self, ctx: LayerContext) -> LayerResult: ...
    def observe(self, obs: Observation) -> None: ...   # optional: learn from an outcome or label
```

`LayerServices` is the narrow set of things a layer may use: a namespaced key/value store (`state.get(key)`, `state.put(key, json)`) backed by a `layer_state(layer, key, value_json)` table, a clock, and a logger. Layers do not get the raw database handle.

`observe` is how a learned layer trains. The core calls it when an outcome or label arrives for a decision the layer took part in, passing the layer's own prediction, the event and the outcome, so the layer can update itself online.

### Rules for classifiers in this version

- A classifier may return **`defer` or `skip`**. It may not return `wake`. A wake is the default; a classifier exists to take wakes away, and an opaque layer that can force a wake could reintroduce what a rule deliberately removed.
- It must return a `confidence` and a `reason` that names the model and the evidence (`bayes:p_wanted=0.012`).
- It must `defer` when it is not sure, and when it has too little training data. "Not enough data" is the normal state for a new layer.
- It runs only on events that rules left as a wake proposal (it is placed between `rules` and the governors), so it never sees events a rule already decided.

## Registration and trust

Config never imports code. A layer type is available only if a **package the user installed** registers it through the `wakegate.layers` entry-point group, or it is built in. `type: bayes` names a registered type; there is no `type: "some.module:Class"`.

This keeps the property that a config file can be reviewed instead of sandboxed: reading a YAML file cannot cause anything to execute. The cost is that adding a layer means installing a package, which is the right amount of friction for code that can drop events.

## Cost and views

`LayerResult.cost` already carries `ms`, `tokens_in`, `tokens_out`, `usd`; the funnel already sums them. A layer that calls a model fills them in and the report shows what each layer costs per event it handled. `views` stay unused until a layer needs to publish derived data.

---

# Part C. Outcomes: the free ground truth

## The signal

After the agent handles a wake, whoever ran it reports one of two things:

- `acted`: the run did something (changed a file, sent a message, opened a ticket, answered).
- `noop`: the run found nothing to do ("HEARTBEAT_OK", "no changes", an empty reply).

```
wakegate outcome <decision-id> acted|noop [--source agent]
```

and the MCP tool `wakegate_outcome`. A harness can call it with one line after each run; an agent can call it on itself. For batched events delivered with a wake, the same call accepts several ids.

A `noop` run is an **unwanted wake in the exact sense this project cares about**: it cost a model invocation and produced nothing. That is why outcomes are the right signal and why they are cheap: they come from what already happened, with no judgement call and no human.

## Storage

```sql
CREATE TABLE outcomes (
  decision_id INTEGER PRIMARY KEY, outcome TEXT NOT NULL,   -- acted | noop
  ts INTEGER NOT NULL, source TEXT NOT NULL                 -- agent | harness | human
);
```

Outcomes are separate from labels on purpose. A label says "this event deserved a wake" and is a judgement; an outcome says "this wake produced something" and is an observation. They agree often and not always (an agent can act on something that was not worth the wake, or correctly do nothing about something worth knowing). Reports show both and never merge them silently.

## Where this can mislead

- **`noop` is not always "should have been skipped".** An agent that checks a dashboard and finds it healthy did the job. If those checks are valuable, they are not noise. The signal measures cost wasted, not value lost; the labels cover value.
- **Selection.** Events a layer skips get no outcome. That is the feedback-loop problem and the reason enforcement requires a canary.
- **Who reports.** If nothing reports outcomes, none of this works. For Rakazo-like systems that already know whether a run produced output, the call is one line. For a bare cron script it is a convention the user has to adopt. This is the main adoption risk of the whole approach.

---

# Part D. The first learned layer and its baseline (step 4b to 4d)

## The baseline first: a rule miner

Before a model, a transparent attempt at the same job:

```
wakegate suggest [--since 14d] [--min-support 20] [--max-acted-rate 0.02]
```

It scans events that woke the agent and have an outcome, finds single-field predicates (`data.actor matches ...`, `source = x`, `data.kind = y`, `key` prefix) where almost every wake was `noop`, and prints them **as YAML rules** with their support and the count of `acted` events they would have dropped, and the result of replaying them over the same history. A person reads them, edits, pastes them into the config. Nothing is applied automatically.

This is the bar the learned layer has to clear. If a handful of readable rules recover most of the avoidable wakes, a model adds opacity for no gain, and the right answer is to stop there.

## The learned layer: online naive Bayes

Chosen because it is the simplest thing that can say "this looks like the noop wakes" and because it needs no dependencies and trains incrementally.

- **Features**: tokens from `source`, `key` with digits collapsed (`pr/412` becomes `pr/#`), `severity`, and every leaf of `data` as `path=value` (strings truncated, numbers bucketed by magnitude, long free text split into words with a cap).
- **Classes**: `acted` and `noop`, learned from outcomes (and from labels when no outcome exists).
- **Output**: `p_acted`. The layer returns `skip` with `confidence = 1 - p_acted` only when **all** hold: at least `min_examples` of each class seen, `p_acted < skip_below` (default 0.02), and the event has no feature never seen in training that rules or severity mark as important (unseen features push towards `defer`). Otherwise `defer`.
- **State**: the counts live in `layer_state`, updated in `observe`. No pickles, no model files: the model is a table of counts anyone can read.
- **Cost**: free. It exists mostly to validate the plumbing (shadow, prequential scoring, canary, promotion check) with a model whose behaviour is easy to inspect.

## The decision gate

Run both in shadow on real traffic that has outcomes. The layer is worth keeping only if it removes a **meaningfully larger** share of `noop` wakes than the mined rules at a false-skip rate within the target. If not, remove it and keep the rules. The synthetic benchmark cannot settle this (see below).

## What the synthetic benchmark can and cannot say

It can check the machinery: that a shadow layer leaves the verdict untouched, that prequential scoring does not leak, that canary attribution and the intervals behave. It cannot say whether a learned layer helps, because in the generated traffic the noise is separable by a single field (`kind: newsletter`), which a rule already catches. A good-looking result there would be an artefact of how the traffic was written. The design therefore treats real outcomes as the only acceptable evidence and says so in the report text.

---

# Plan

Each step ships on its own and keeps the golden tests green.

1. **3a. Modes in the pipeline and `layers:` config.** *Done.* Shadow layers do not affect the cascade; ordering and validation as above; no change when `layers:` is absent (the golden tests still pass). The funnel marks shadow layers and counts what they would have skipped.
2. **3b. Layer-attributed canary and `review --layer`.** *Done.* `canary.layers: {name: rate}` overrides the global rate for events that layer skipped (the deciding layer is stored per decision, `decision_meta.decided_by`). Because layers can be sampled at different rates, the overall estimate is **stratified by layer** and weighted by how much each layer suppressed, instead of pooling the samples; a layer with no labelled sample is reported as uncovered, not extrapolated. One sampled layer still gives the Wilson interval; several use a normal approximation to the stratified estimator with a smoothed variance. `report` has a `byLayer` section next to `byRule`.
3. **4a. Plugin interface.** `create`, `observe`, `params_schema`, entry-point registry, `layer_state`.
4. **C. Outcomes.** `outcomes` table, `wakegate outcome`, `wakegate_outcome`, outcome counts in the report and funnel.
5. **3c. Shadow evaluation report.** Prequential scoring, intervals, advisory promotion check.
6. **4c. `wakegate suggest`.** The baseline.
7. **4d. Naive Bayes layer** in shadow, compared with 6 on real data.

Steps 1 to 5 are useful even if the learned layer is abandoned: they give any future layer, including an LLM triage layer, a safe way in. Steps 6 and 7 are the experiment.

# Open questions

1. ~~Marginal counterfactual.~~ Accepted.
2. **The wrapper's contract.** Decided to build it (see above). Still to settle: how `acted` and `noop` are told apart for a command that always prints something (`--noop-match`, an exit code, an empty stdout).
3. **Outcome semantics for agents that always reply.** An agent that always writes a status line never produces `noop`. The convention (for example, an empty reply or a fixed phrase means `noop`) has to be set by the user. Provide a `--noop-match` pattern for the wrapper.
4. **Defaults** (`min_examples`, `skip_below`, `max_false_skip`, `min_outcomes`, canary floor of 2%) are guesses to be tuned on real data.
5. **Timeouts** for slow layers are deferred. Layers needing network access must apply their own client timeouts and fail open; a per-layer wall-clock limit would need a worker thread, and SQLite handles are thread-bound.
6. ~~Classifiers may only skip or defer.~~ Accepted for v1.
7. **Rule miner scope.** Single-field predicates only, or pairs? Single fields keep the output readable and the search small.
