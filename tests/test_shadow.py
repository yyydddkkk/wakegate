import sqlite3

import pytest

from conftest import MIN, T0, ev
from wakegate import Gate, Store, parse_config, replay
from wakegate.queries import catchup, report, review, wilson

SKIP_NOISE = """
rules: [{ name: noise, when: { path: data.kind, eq: noise }, then: skip }]
"""


def noise(i, data=None):
    return ev("s", f"k{i}", T0 + i * MIN, {**(data or {"kind": "noise"}), "i": i})


def test_shadow_everything_wakes_the_verdict_is_recorded_nothing_is_batched(setup):
    gate, store = setup(f"mode: shadow\n{SKIP_NOISE}cooldownSeconds: 600")
    n = gate.decide(noise(1))
    assert (n.action, n.verdict, n.mode, n.reason) == ("wake", "skip", "shadow", "rule:noise")

    gate.decide(ev("s", "x", T0, {"kind": "real", "n": 1}))
    cooled = gate.decide(ev("s", "x", T0 + MIN, {"kind": "real", "n": 2}))
    assert (cooled.action, cooled.verdict) == ("wake", "batch")  # cooldown is simulated from verdicts
    assert store.pending_count() == 0, "nothing is held back in shadow mode"


def test_shadow_simulates_the_budget_from_verdicts_so_it_matches_enforcement():
    cfg = "budget: { maxWakesPerDay: 2, timezone: UTC }"

    def run(yaml):
        g = Gate(parse_config(yaml), Store(":memory:"))
        return [g.decide(noise(i, {"kind": "real"})).verdict for i in (1, 2, 3, 4)]

    assert run(f"mode: shadow\n{cfg}") == run(cfg) == ["wake", "wake", "batch", "batch"]


def test_config_rejects_a_canary_in_shadow_mode_and_a_bad_rate():
    with pytest.raises(ValueError, match="no effect in shadow mode"):
        parse_config("mode: shadow\ncanary: { rate: 0.1 }")
    with pytest.raises(ValueError, match="between 0 and 1"):
        parse_config("canary: { rate: 2 }")
    with pytest.raises(ValueError, match="enforce.*shadow"):
        parse_config("mode: dry")


def test_canary_releases_a_would_be_skip_as_a_wake_names_the_id_and_never_releases_duplicates(setup):
    gate, _ = setup(f"canary: {{ rate: 1 }}\n{SKIP_NOISE}")
    d = gate.decide(noise(1))
    assert (d.action, d.verdict, d.mode, d.canary) == ("wake", "skip", "canary", True)
    assert f"label {d.id} wanted|unwanted" in d.note

    dup = gate.decide(noise(1))
    assert dup.reason == "duplicate"
    assert dup.action == "skip", "duplicates carry no new information and are never canaried"


def test_canary_rate_zero_releases_nothing_rate_quarter_about_a_quarter_reproducibly():
    def releases(yaml):
        g = Gate(parse_config(yaml), Store(":memory:"))
        return sum(g.decide(noise(i)).mode == "canary" for i in range(2000))

    assert releases(SKIP_NOISE) == 0
    a = releases(f"canary: {{ rate: 0.25 }}\n{SKIP_NOISE}")
    assert 400 < a < 600, f"expected about 500, got {a}"
    assert releases(f"canary: {{ rate: 0.25 }}\n{SKIP_NOISE}") == a, "same input, same canaries"


def test_a_canary_does_not_spend_the_daily_budget(setup):
    gate, _ = setup(f"canary: {{ rate: 1 }}\nbudget: {{ maxWakesPerDay: 1, timezone: UTC }}\n{SKIP_NOISE}")
    gate.decide(noise(1))  # canary
    gate.decide(noise(2))  # canary
    assert gate.decide(noise(3, {"kind": "real"})).action == "wake", "the one real wake is still available"


def test_labels_from_canaries_and_the_review_queue_count_as_samples_others_do_not(setup):
    gate, store = setup(f"canary: {{ rate: 0 }}\n{SKIP_NOISE}")
    a = gate.decide(noise(1))  # plain skip
    assert store.set_label(a.id, "wanted", None, T0) is False
    b = gate.decide(noise(2))
    store.add_to_queue([b.id], T0)
    assert store.set_label(b.id, "wanted", None, T0) is True
    with pytest.raises(ValueError, match="no decision"):
        store.set_label(9999, "wanted", None, T0)

    c_gate, c_store = setup(f"canary: {{ rate: 1 }}\n{SKIP_NOISE}")
    d = c_gate.decide(noise(3))
    assert c_store.set_label(d.id, "unwanted", None, T0) is True


def test_wilson_interval_matches_known_values():
    lo0, hi0 = wilson(0, 10)
    assert lo0 == 0 and abs(hi0 - 0.2775) < 0.001
    lo, hi = wilson(5, 10)
    assert abs(lo - 0.2366) < 0.001 and abs(hi - 0.7634) < 0.001
    assert wilson(0, 0) == (0.0, 1.0)


def test_report_turns_labelled_canaries_into_a_miss_estimate_with_an_interval(setup):
    gate, store = setup(f"canary: {{ rate: 1 }}\n{SKIP_NOISE}")
    ids = [gate.decide(noise(i)).id for i in range(40)]
    assert report(store, T0 - MIN)["missEstimate"] is None
    assert report(store, T0 - MIN)["unlabeledSamples"] == 40

    for i, id_ in enumerate(ids):
        store.set_label(id_, "wanted" if i < 10 else "unwanted", None, T0)
    r = report(store, T0 - MIN)
    est = r["missEstimate"]
    assert est["sampleSize"] == 40 and est["rate"] == 0.25
    assert est["ci95"][0] < 0.25 < est["ci95"][1]
    assert r["suppressedByRules"] == 40
    assert r["unlabeledSamples"] == 0
    assert not any("labelled random samples" in a for a in r["advice"]), "40 labels is enough to stop warning"


def test_report_breaks_the_estimate_down_by_the_rule_that_suppressed_the_event(setup):
    gate, store = setup("""canary: { rate: 1 }
rules:
  - { name: noise, when: { path: data.kind, eq: noise }, then: skip }
  - { name: too-broad, when: { path: data.kind, eq: alert }, then: skip }
""")
    for i in range(10):
        store.set_label(gate.decide(noise(i, {"kind": "noise"})).id, "unwanted", None, T0)
        store.set_label(gate.decide(noise(100 + i, {"kind": "alert"})).id, "wanted" if i < 6 else "unwanted", None, T0)
    r = report(store, T0 - 200 * MIN)
    by = {x["rule"]: x for x in r["byRule"]}
    assert by["too-broad"]["wanted"] == 6 and by["too-broad"]["sampleSize"] == 10
    assert by["noise"]["wanted"] == 0
    assert r["byRule"][0]["rule"] == "too-broad", "the rule hiding wanted events comes first"


def test_report_warns_when_the_sample_is_small_and_ignores_non_random_labels(setup):
    gate, store = setup(SKIP_NOISE)  # canary off
    d = gate.decide(noise(1))
    store.set_label(d.id, "wanted", None, T0)  # manual label, not a sample
    r = report(store, T0 - MIN)
    assert r["missEstimate"] is None
    assert r["labels"]["wanted"] == 1
    assert "labelled random samples" in " ".join(r["advice"])


def test_shadow_report_counts_avoidable_wakes_and_prices_them(setup):
    gate, store = setup(f"mode: shadow\n{SKIP_NOISE}")
    for i in range(5):
        gate.decide(noise(i))
    gate.decide(ev("s", "real", T0, {"kind": "real"}))
    r = report(store, T0 - MIN, cost_per_wake=2)
    assert r["byMode"]["shadow"] == 6
    assert r["wakesAvoidable"] == 5
    assert r["estimatedSaving"] == 10


def test_review_draws_a_seeded_random_sample_never_duplicates_canaries_or_labelled_ones(setup):
    gate, store = setup(SKIP_NOISE)
    ids = [gate.decide(noise(i)).id for i in range(30)]
    gate.decide(noise(0))  # a duplicate
    store.set_label(ids[0], "unwanted", None, T0)

    first = review(store, T0 - MIN, sample=5, seed=7)
    assert len(first) == 5
    assert all(b["reason"] == "rule:noise" and b["id"] != ids[0] for b in first)
    # Asking again without a new draw returns the same waiting items.
    assert [b["id"] for b in review(store, T0 - MIN)] == [b["id"] for b in first]
    # Labelling removes an item from the waiting list.
    store.set_label(first[0]["id"], "unwanted", None, T0)
    assert len(review(store, T0 - MIN)) == 4
    # Same seed on a fresh store picks the same events.
    g2, s2 = setup(SKIP_NOISE)
    for i in range(30):
        g2.decide(noise(i))
    g2.decide(noise(0))
    s2.set_label(ids[0], "unwanted", None, T0)
    assert [b["id"] for b in review(s2, T0 - MIN, sample=5, seed=7)] == [b["id"] for b in first]


def test_catchup_follows_the_verdict_so_shadow_mode_still_hides_rule_skipped_noise(setup):
    gate, store = setup(f"mode: shadow\n{SKIP_NOISE}")
    gate.decide(noise(1))
    gate.decide(ev("s", "real", T0, {"kind": "real"}))
    c = catchup(store, T0 - MIN)
    assert [x["key"] for x in c["changed"]] == ["real"]
    assert c["filteredBySkipRules"] == 1


def test_replay_counts_verdicts_and_charges_canaries_as_wakes():
    events = [noise(i) for i in range(200)]
    none = replay(events, parse_config(SKIP_NOISE), 1)
    assert none.counts["skip"] == 200 and none.canaries_released == 0
    some = replay(events, parse_config(f"canary: {{ rate: 0.5 }}\n{SKIP_NOISE}"), 1)
    assert 60 < some.canaries_released < 140
    assert some.gated_cost == some.canaries_released
    shadow = replay(events, parse_config(f"mode: shadow\n{SKIP_NOISE}"), 1)
    assert shadow.counts["skip"] == 200, "shadow replay shows what enforcing would do"


def test_a_database_from_before_shadow_canary_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
    CREATE TABLE decisions (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, day TEXT NOT NULL,
      source TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
      action TEXT NOT NULL, reason TEXT NOT NULL, rule TEXT, changed INTEGER NOT NULL, event_json TEXT NOT NULL
    );
    INSERT INTO decisions (ts, day, source, key, fingerprint, action, reason, rule, changed, event_json)
    VALUES (1, '2026-01-05', 's', 'k', 'f', 'skip', 'rule:x', 'x', 1, '{"source":"s","key":"k"}');
    """)
    old.commit()
    old.close()

    store = Store(path)
    row = store.get_decision(1)
    assert row.verdict == "skip", "verdict is backfilled from action"
    assert row.mode == "enforce"
    store.close()
