import json
import sqlite3
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from conftest import MIN, T0, ev
from wakegate import Gate, Store, parse_config
from wakegate.trace import build_trace

SCHEMA = json.loads((Path(__file__).parent.parent / "schemas" / "trace.v1.schema.json").read_text())
validator = Draft202012Validator(SCHEMA)

CONFIG = r"""
ignorePaths: [checkedAt]
bypass: [{ name: critical, when: { path: severity, eq: critical } }]
rules:
  - { name: ignore-bots, when: { path: data.actor, matches: "\\[bot\\]$" }, then: skip }
  - { name: review, when: { path: data.state, eq: review_requested }, then: wake, exempt: [cooldown, budget] }
cooldownSeconds: 600
budget: { maxWakesPerDay: 2, timezone: UTC, quietHours: { start: "22:00", end: "23:00" } }
"""


def trace_of(gate, store, event):
    d = gate.decide(event)
    t = build_trace(store, d.id)
    validator.validate(t)  # every trace conforms to the published schema
    return d, t


def names(t):
    return [(layer["layer"], layer["outcome"], layer["reason"]) for layer in t["layers"]]


def test_a_duplicate_stops_at_the_first_layer(setup):
    gate, store = setup(CONFIG)
    gate.decide(ev("s", "k", T0, {"v": 1}))
    _, t = trace_of(gate, store, ev("s", "k", T0 + MIN, {"v": 1}))
    assert names(t) == [("dedupe", "skip", "duplicate")]
    assert t["result"]["decided_by"] == "dedupe" and t["layers"][0]["final"] is True


def test_the_cascade_stops_at_the_first_final_layer_and_later_layers_are_absent(setup):
    gate, store = setup(CONFIG)
    _, t = trace_of(gate, store, ev("s", "k", T0, {"actor": "ci[bot]"}))
    assert names(t) == [("dedupe", "defer", "first-seen"), ("bypass", "defer", "no-match"), ("rules", "skip", "rule:ignore-bots")]
    assert t["result"] == {"verdict": "skip", "decided_by": "rules", "delivered": "skip", "mode": "enforce", "reason": "rule:ignore-bots"}


def test_governors_run_only_on_a_non_final_wake_and_record_what_they_checked(setup):
    gate, store = setup(CONFIG)
    _, t = trace_of(gate, store, ev("s", "k", T0, {"v": 1}))
    assert [layer["layer"] for layer in t["layers"]] == ["dedupe", "bypass", "rules", "cooldown", "quiet_hours", "budget"]
    budget = t["layers"][-1]
    assert budget["detail"] == {"used": 0, "max": 2} and budget["outcome"] == "defer"
    assert t["result"]["decided_by"] == "rules" and t["result"]["verdict"] == "wake"

    # A bypass is final, so no governor runs.
    _, b = trace_of(gate, store, ev("s", "p", T0, {"x": 1}, "critical"))
    assert [layer["layer"] for layer in b["layers"]] == ["dedupe", "bypass"]
    assert b["layers"][1]["outcome"] == "wake" and b["layers"][1]["rule"] == "critical"


def test_a_governor_that_blocks_is_the_decider_and_keeps_the_proposing_rule(setup):
    gate, store = setup(CONFIG)
    gate.decide(ev("s", "k", T0, {"v": 1}))
    d, t = trace_of(gate, store, ev("s", "k", T0 + 5 * MIN, {"v": 2}))
    assert (d.action, d.reason) == ("batch", "cooldown")
    assert t["result"]["decided_by"] == "cooldown"
    cooldown = t["layers"][-1]
    assert cooldown["outcome"] == "batch" and cooldown["final"] is True
    assert cooldown["detail"]["since_last_wake_ms"] == 5 * MIN


def test_an_exemption_is_visible_in_the_trace(setup):
    gate, store = setup(CONFIG)
    gate.decide(ev("s", "k", T0, {"v": 1}))
    _, t = trace_of(gate, store, ev("s", "k", T0 + MIN, {"state": "review_requested"}))
    rules = next(layer for layer in t["layers"] if layer["layer"] == "rules")
    assert rules["detail"]["exempt"] == ["cooldown", "budget"]
    by_name = {layer["layer"]: layer for layer in t["layers"]}
    assert by_name["cooldown"]["reason"] == "exempt" and by_name["budget"]["reason"] == "exempt"
    assert t["result"]["verdict"] == "wake"


def test_layers_that_could_never_act_are_not_in_the_pipeline(setup):
    gate, store = setup("dedupe: false\nrules: [{ name: r, when: { path: source, eq: x }, then: skip }]")
    _, t = trace_of(gate, store, ev("s", "k", T0, 1))
    assert [layer["layer"] for layer in t["layers"]] == ["rules"]


def test_shadow_marks_every_layer_as_not_applied(setup):
    gate, store = setup("mode: shadow\n" + CONFIG)
    d, t = trace_of(gate, store, ev("s", "k", T0, {"actor": "ci[bot]"}))
    assert d.action == "wake" and t["result"]["verdict"] == "skip" and t["result"]["delivered"] == "wake"
    assert all(layer["mode"] == "shadow" and layer["applied"] is False for layer in t["layers"])


def test_a_canary_marks_the_overridden_layer(setup):
    gate, store = setup("canary: { rate: 1 }\n" + CONFIG)
    d, t = trace_of(gate, store, ev("s", "k", T0, {"actor": "ci[bot]"}))
    assert d.canary and t["result"]["mode"] == "canary" and t["result"]["verdict"] == "skip"
    rules = t["layers"][-1]
    assert rules["layer"] == "rules" and rules["mode"] == "canary" and rules["applied"] is False
    assert all(layer["applied"] is True for layer in t["layers"][:-1])


def test_a_layer_that_raises_fails_open(setup):
    from wakegate.builtin_layers import default_layers
    from wakegate.layers import Layer

    class Broken(Layer):
        name, kind = "broken", "classifier"

        def run(self, ctx):
            raise RuntimeError("model unreachable")

    store = Store(":memory:")
    cfg = parse_config("version: 1")
    layers = default_layers(cfg)
    layers.insert(len(layers) - 1, Broken())  # before the rules layer
    gate = Gate(cfg, store, layers=layers)
    d, t = trace_of(gate, store, ev("s", "k", T0, 1))
    broken = next(layer for layer in t["layers"] if layer["layer"] == "broken")
    assert broken["outcome"] == "error" and broken["detail"]["type"] == "RuntimeError"
    assert d.action == "wake", "a broken filter must cost money, not drop events"
    # ...and the rules layer still ran after it.
    assert t["layers"][-1]["layer"] == "rules"


def test_on_error_skip_is_honoured_when_a_layer_opts_in(setup):
    from wakegate.builtin_layers import default_layers
    from wakegate.layers import Layer

    class Strict(Layer):
        name, kind, on_error = "strict", "rules", "skip"

        def run(self, ctx):
            raise RuntimeError("x")

    store = Store(":memory:")
    cfg = parse_config("version: 1")
    gate = Gate(cfg, store, layers=[Strict(), *default_layers(cfg)])
    d, _ = trace_of(gate, store, ev("s", "k", T0, 1))
    assert (d.action, d.reason) == ("skip", "error:strict")


def test_a_decision_made_before_traces_existed_gets_a_synthesized_trace(tmp_path):
    path = tmp_path / "old.db"
    store = Store(path)
    store._db.execute(
        "INSERT INTO decisions (ts, day, source, key, fingerprint, action, verdict, mode, reason, rule, changed, event_json)"
        " VALUES (1, '2026-01-05', 's', 'k', 'f', 'skip', 'skip', 'enforce', 'rule:x', 'x', 1, '{\"source\":\"s\",\"key\":\"k\"}')"
    )
    t = build_trace(store, 1)
    validator.validate(t)
    assert t["synthesized"] is True and t["layers"] == [] and t["result"]["reason"] == "rule:x"
    with pytest.raises(ValueError, match="no decision"):
        build_trace(store, 99)


def test_trace_recording_is_rolled_back_with_the_decision(setup):
    gate, store = setup("version: 1")

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with store.transaction():
            gate.decide(ev("s", "k", T0, 1))
            raise Boom
    assert store._db.execute("SELECT COUNT(*) FROM layer_runs").fetchone()[0] == 0
    assert store._db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
