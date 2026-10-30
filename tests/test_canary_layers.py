import pytest

from conftest import MIN, T0, ev
from test_inspect import run_cli
from wakegate import Gate, Store, parse_config
from wakegate.layers import Layer, LayerResult
from wakegate.queries import report, review
from wakegate.registry import register_layer_type, unregister_layer_type


class Junk(Layer):
    """A stand-in classifier that skips events marked junk."""

    kind = "classifier"

    def run(self, ctx):
        if (ctx.event.data or {}).get("junk"):
            return LayerResult("skip", False, "stub:junk", confidence=0.99)
        return LayerResult("defer", False, "stub:unsure")


@pytest.fixture(autouse=True)
def types():
    register_layer_type("junk", Junk)
    yield
    unregister_layer_type("junk")


PIPELINE = """
rules: [{ name: noise, when: { path: data.kind, eq: noise }, then: skip }]
layers: [dedupe, rules, { name: nb, type: junk, mode: enforce }]
"""


def make(canary: str):
    store = Store(":memory:")
    return Gate(parse_config(f"{canary}\n{PIPELINE}"), store), store


def noise(i):
    return ev("s", f"n{i}", T0 + i * MIN, {"kind": "noise", "i": i})


def junk(i):
    return ev("s", f"j{i}", T0 + (1000 + i) * MIN, {"junk": True, "i": i})


def test_a_layers_own_rate_decides_which_of_its_skips_are_released():
    # nb at 100%, everything else at 0: only the classifier's skips come back as canaries.
    gate, _ = make("canary: { rate: 0, layers: { nb: 1 } }")
    rules_skip, nb_skip = gate.decide(noise(1)), gate.decide(junk(1))
    assert (rules_skip.action, rules_skip.canary) == ("skip", False)
    assert (nb_skip.action, nb_skip.canary) == ("wake", True)
    assert "the nb layer would have skipped" in nb_skip.note

    # The reverse: global 100%, but rules switched off for canaries.
    gate, _ = make("canary: { rate: 1, layers: { rules: 0 } }")
    assert gate.decide(noise(1)).action == "skip"
    assert gate.decide(junk(1)).canary is True


def test_canary_layers_is_validated():
    with pytest.raises(ValueError, match='"ghost" is not a layer in this pipeline'):
        parse_config("canary: { layers: { ghost: 0.5 } }")
    with pytest.raises(ValueError, match="between 0 and 1"):
        parse_config("canary: { layers: { rules: 2 } }")
    with pytest.raises(ValueError, match="no effect in shadow mode"):
        parse_config("mode: shadow\ncanary: { layers: { rules: 0.5 } }")
    with pytest.raises(ValueError, match="canary.layers for this layer"):
        parse_config("canary: { rate: 0.5, layers: { nb: 0 } }\n" + PIPELINE)  # the layer's own rate wins
    parse_config("canary: { rate: 0, layers: { nb: 0.05 } }\n" + PIPELINE)


def test_the_estimate_is_stratified_by_layer_not_pooled():
    gate, store = make("canary: { rate: 1 }")
    rules_ids = [gate.decide(noise(i)).id for i in range(200)]
    nb_ids = [gate.decide(junk(i)).id for i in range(50)]
    # 10% wanted among rules' skips, 50% among nb's, but nb's are 20% of what was suppressed.
    for i, id_ in enumerate(rules_ids[:20]):
        store.set_label(id_, "wanted" if i < 2 else "unwanted", None, T0)
    for i, id_ in enumerate(nb_ids[:20]):
        store.set_label(id_, "wanted" if i < 10 else "unwanted", None, T0)

    r = report(store, T0 - MIN)
    est = r["missEstimate"]
    assert est["method"] == "stratified" and est["layers"] == ["nb", "rules"]
    assert abs(est["rate"] - (0.8 * 0.1 + 0.2 * 0.5)) < 1e-9, "weighted by how much each layer suppressed (0.18), not pooled (0.30)"
    assert est["ci95"][0] < 0.18 < est["ci95"][1]
    assert est["sampleSize"] == 40 and est["wanted"] == 12
    assert est["expectedMissedEvents"] == [0, 0], "at a 100% canary nothing was actually lost"

    by = {x["layer"]: x for x in r["byLayer"]}
    assert (by["rules"]["suppressed"], by["rules"]["sampleSize"], by["rules"]["wanted"]) == (200, 20, 2)
    assert (by["nb"]["suppressed"], by["nb"]["sampleSize"], by["nb"]["wanted"]) == (50, 20, 10)
    assert r["byLayer"][0]["layer"] == "nb", "the layer hiding wanted events comes first"


def test_a_layer_with_no_sample_is_reported_not_extrapolated():
    gate, store = make("canary: { rate: 1 }")
    ids = [gate.decide(noise(i)).id for i in range(10)]
    for id_ in ids:
        store.set_label(id_, "unwanted", None, T0)
    for i in range(7):
        gate.decide(junk(i))  # nb suppressed events, none labelled
    est = report(store, T0 - MIN)["missEstimate"]
    assert est["layers"] == ["rules"] and est["method"] == "pooled"
    assert est["uncoveredSuppressed"] == 7


def test_review_can_draw_from_one_layer_and_its_labels_count_for_that_layer():
    gate, store = make("canary: { rate: 0, layers: { nb: 0.02 } }")
    for i in range(60):
        gate.decide(noise(i))
    nb_events = [gate.decide(junk(i)) for i in range(60)]
    canaries = sum(1 for d in nb_events if d.canary)
    assert canaries >= 1, "the 2% canary released a few; they are waiting for a label too"

    items = review(store, T0 - MIN, sample=15, seed=3, layer="nb")
    # The draw (15) plus the canaries that nobody has labelled yet.
    assert len(items) == 15 + canaries and all(b["decidedBy"] == "nb" for b in items)
    drawn = [b for b in items if b["id"] in set(store.queued_ids())]
    assert len(drawn) == 15

    for b in items:
        store.set_label(b["id"], "unwanted", None, T0)
    by = {x["layer"]: x for x in report(store, T0 - MIN)["byLayer"]}
    assert by["nb"]["sampleSize"] == 15 + canaries and by["rules"]["sampleSize"] == 0


def test_review_for_a_layer_with_nothing_suppressed_returns_nothing():
    gate, store = make("canary: { rate: 0, layers: { nb: 0.02 } }")
    gate.decide(noise(1))
    assert review(store, T0 - MIN, sample=5, seed=1, layer="nb") == []


def test_cli_review_layer_and_report_show_the_layer(tmp_path, capsys):
    cfg = tmp_path / "wakegate.yaml"
    cfg.write_text("canary: { rate: 0, layers: { nb: 0.02 } }\n" + PIPELINE)
    import json

    for i in range(6):
        run_cli(tmp_path, capsys, "check", "--event", json.dumps({"source": "s", "key": f"n{i}", "data": {"kind": "noise", "i": i}}))
    code, out = run_cli(tmp_path, capsys, "review", "--sample", "3", "--seed", "1", "--layer", "rules")
    assert code == 0 and out.out.count("[rules]") == 3
    import re

    first = re.search(r"^#(\d+)", out.out, re.M).group(1)
    code, out = run_cli(tmp_path, capsys, "label", first, "wanted")
    code, out = run_cli(tmp_path, capsys, "report")
    assert "layer rules: 1 of 1 sampled were wanted" in out.out


def test_decision_rows_know_which_layer_decided(setup):
    gate, store = setup("rules: [{ name: r, when: { path: data.x, eq: 1 }, then: skip }]")
    gate.decide(ev("s", "a", T0, {"x": 1}))
    gate.decide(ev("s", "a", T0 + MIN, {"x": 1}))
    assert [r.decided_by for r in store.recent_decisions(5)] == ["rules", "dedupe"]
