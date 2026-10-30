import pytest

from conftest import MIN, T0, ev
from wakegate import Gate, Store, parse_config
from wakegate.layers import Layer, LayerResult
from wakegate.queries import report
from wakegate.registry import register_layer_type, unregister_layer_type
from wakegate.trace import build_trace


class SkipsAnythingWith(Layer):
    """A stand-in classifier: skips events whose data has `junk: true`, defers otherwise."""

    kind = "classifier"

    def run(self, ctx):
        if (ctx.event.data or {}).get("junk"):
            return LayerResult("skip", False, "stub:junk", confidence=0.99)
        return LayerResult("defer", False, "stub:unsure")


class Wrongly(Layer):
    kind = "classifier"

    @classmethod
    def create(cls, name, params):
        inst = cls()
        inst.out = params.get("out", "wake")
        return inst

    def run(self, ctx):
        return LayerResult(self.out, True, "stub:oops")


class Raises(Layer):
    kind = "classifier"

    def run(self, ctx):
        raise RuntimeError("model unreachable")


@pytest.fixture(autouse=True)
def types():
    register_layer_type("stub", SkipsAnythingWith)
    register_layer_type("wrongly", Wrongly)
    register_layer_type("raises", Raises)
    yield
    for t in ("stub", "wrongly", "raises"):
        unregister_layer_type(t)


def cfg(layers: str, extra: str = "") -> str:
    return f"{extra}\nlayers:\n{layers}\n"


FULL = """  - dedupe
  - rules
  - { name: nb, type: stub, mode: %s }
  - cooldown
"""
BASE = "cooldownSeconds: 600"


def verdicts(yaml, events):
    store = Store(":memory:")
    gate = Gate(parse_config(yaml), store)
    return [gate.decide(e).verdict for e in events], store, gate


def test_without_a_layers_section_nothing_changes():
    assert parse_config("cooldownSeconds: 600").layer_specs is None


def test_a_shadow_layer_is_recorded_but_does_not_change_the_verdict_or_stop_the_cascade():
    events = [ev("s", "a", T0, {"junk": True}), ev("s", "b", T0 + MIN, {"v": 1})]
    plain, _, _ = verdicts(BASE, events)
    shadowed, store, gate = verdicts(cfg(FULL % "shadow", BASE), events)
    assert shadowed == plain == ["wake", "wake"]

    t = build_trace(store, 1)
    assert [x["layer"] for x in t["layers"]] == ["dedupe", "rules", "nb", "cooldown"], "later layers still ran"
    nb = t["layers"][2]
    assert (nb["outcome"], nb["mode"], nb["applied"], nb["confidence"]) == ("skip", "shadow", False, 0.99)
    assert t["result"]["decided_by"] == "rules" and t["result"]["verdict"] == "wake"


def test_an_enforced_classifier_skips_and_ends_the_cascade():
    v, store, _ = verdicts(cfg(FULL % "enforce", BASE + "\ncanary: { rate: 0.02 }"), [ev("s", "a", T0, {"junk": True})])
    assert v == ["skip"]
    t = build_trace(store, 1)
    assert [x["layer"] for x in t["layers"]] == ["dedupe", "rules", "nb"], "cooldown never ran"
    assert t["layers"][2]["final"] is True and t["result"]["decided_by"] == "nb"


def test_a_layer_set_to_off_is_not_in_the_pipeline():
    _, store, _ = verdicts(cfg(FULL % "off", BASE), [ev("s", "a", T0, {"junk": True})])
    assert [x["layer"] for x in build_trace(store, 1)["layers"]] == ["dedupe", "rules", "cooldown"]


def test_a_classifier_only_sees_events_that_rules_left_as_a_wake_proposal():
    yaml = cfg("""  - dedupe
  - rules
  - { name: nb, type: stub, mode: shadow }
""", "rules: [{ name: junk, when: { path: data.junk, eq: true }, then: skip }]")
    _, store, _ = verdicts(yaml, [ev("s", "a", T0, {"junk": True}), ev("s", "b", T0, {"v": 1})])
    assert "nb" not in [x["layer"] for x in build_trace(store, 1)["layers"]], "a rule had already decided"
    assert "nb" in [x["layer"] for x in build_trace(store, 2)["layers"]]


def test_a_shadow_layer_that_raises_is_recorded_and_ignored():
    yaml = cfg("  - dedupe\n  - rules\n  - { name: r, type: raises, mode: shadow }\n")
    v, store, _ = verdicts(yaml, [ev("s", "a", T0, 1)])
    t = build_trace(store, 1)
    assert v == ["wake"] and t["layers"][2]["outcome"] == "error" and t["layers"][2]["applied"] is False


def test_an_enforced_classifier_that_returns_wake_or_batch_is_treated_as_broken_and_fails_open():
    for out in ("wake", "batch"):
        yaml = cfg("  - dedupe\n  - rules\n  - { name: w, type: wrongly, params: { out: %s } }\n" % out, "canary: { rate: 0.05 }")
        v, store, _ = verdicts(yaml, [ev("s", "a", T0, 1)])
        row = build_trace(store, 1)["layers"][2]
        assert v == ["wake"] and row["outcome"] == "error" and "only skip or defer" in row["detail"]["message"]


def test_built_in_governors_can_be_reordered():
    # quiet hours before the budget: the first limit that blocks is the one reported
    yaml = """
budget: { maxWakesPerDay: 0, quietHours: { start: "00:00", end: "23:59" }, timezone: UTC }
layers: [dedupe, rules, %s]
"""
    a = Gate(parse_config(yaml % "quiet_hours, budget"), Store(":memory:")).decide(ev("s", "k", T0, 1))
    b = Gate(parse_config(yaml % "budget, quiet_hours"), Store(":memory:")).decide(ev("s", "k", T0, 1))
    assert (a.reason, b.reason) == ("quiet-hours", "daily-budget")


@pytest.mark.parametrize("yaml, message", [
    ("layers: [dedupe, rules, nope]", 'not a built-in layer'),
    ("layers: [dedupe, rules, dedupe]", 'duplicate layer name "dedupe"'),
    ("layers: [dedupe]", '"rules" is required'),
    ("layers: [rules, dedupe]", "out of order"),
    ("layers: [dedupe, rules, cooldown]", "cooldownSeconds is not set"),
    ("layers: [dedupe, rules, budget]", "budget.maxWakesPerDay is not set"),
    ("cooldownSeconds: 5\nlayers: [cooldown, rules]", "out of order"),
    ("cooldownSeconds: 5\nlayers: [dedupe, rules, cooldown, { name: x, type: stub, mode: shadow }]", "out of order"),
    ("layers: [dedupe, rules, { name: x, type: nosuch, mode: shadow }]", 'unknown layer type "nosuch"; registered types:'),
    ("layers: [dedupe, rules, { name: x, type: stub, mode: loud }]", 'expected "off", "shadow" or "enforce"'),
    ("layers: [dedupe, rules, { name: x, type: stub, mode: shadow, extra: 1 }]", 'unknown key "extra"'),
    ("layers: [dedupe, rules, { name: x, type: stub, mode: shadow, params: [1] }]", "params: expected a mapping"),
    ("layers: [{ name: dedupe, params: { a: 1 } }, rules]", "built-in layers take no params"),
    ("layers: []", "non-empty list"),
    ("layers: [dedupe, rules, { name: x, type: stub, mode: enforce }]", "needs a canary of at least 2%"),
])
def test_layers_config_is_validated(yaml, message):
    with pytest.raises(ValueError, match=message):
        parse_config(yaml)


def test_an_enforced_classifier_is_accepted_once_a_canary_is_configured():
    parse_config("canary: { rate: 0.02 }\nlayers: [dedupe, rules, { name: x, type: stub, mode: enforce }]")


def test_a_stub_that_takes_no_params_rejects_them():
    with pytest.raises(ValueError, match='takes no params'):
        Gate(parse_config("layers: [dedupe, rules, { name: x, type: stub, mode: shadow, params: { a: 1 } }]"), Store(":memory:"))


def test_the_funnel_marks_shadow_layers_and_counts_what_they_would_have_skipped():
    yaml = cfg("  - dedupe\n  - rules\n  - { name: nb, type: stub, mode: shadow }\n")
    store = Store(":memory:")
    gate = Gate(parse_config(yaml), store)
    for i in range(10):
        gate.decide(ev("s", f"k{i}", T0 + i * MIN, {"junk": i < 4, "i": i}))
    f = {x["layer"]: x for x in report(store, T0 - MIN)["funnel"]}
    assert (f["nb"]["mode"], f["nb"]["entered"], f["nb"]["wouldSkip"]) == ("shadow", 10, 4)
    assert f["nb"]["skip"] == 0, "it ended nothing"
    assert f["rules"]["wake"] == 10 and f["rules"]["mode"] == "enforce"


def test_yaml_scalars_follow_yaml_1_2_not_1_1():
    c = parse_config("""
rules:
  - { name: on-state, when: { path: data.state, eq: on }, then: skip }
  - { name: yes-state, when: { path: data.answer, eq: yes }, then: skip }
  - { name: real-bool, when: { path: data.flag, eq: true }, then: skip }
budget: { quietHours: { start: 22:00, end: 07:00 }, timezone: UTC }
layers: [dedupe, rules, { name: x, type: stub, mode: off }]
""")
    assert c.budget.quiet_hours == ("22:00", "07:00"), "unquoted times stay strings"
    assert c.layer_specs[2].mode == "off", "`off` is the word, not the boolean false"
    by = {r.name: r for r in c.rules}
    assert by["on-state"].when({"data": {"state": "on"}}) is True
    assert by["on-state"].when({"data": {"state": True}}) is False
    assert by["yes-state"].when({"data": {"answer": "yes"}}) is True
    assert by["real-bool"].when({"data": {"flag": True}}) is True
    assert parse_config("cooldownSeconds: 1.5").cooldown_seconds == 1.5
    assert parse_config("budget: { maxWakesPerDay: 12 }").budget.max_wakes_per_day == 12
