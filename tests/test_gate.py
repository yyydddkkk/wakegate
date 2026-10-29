import pytest

from conftest import MIN, T0, ev
from wakegate import parse_config, replay
from wakegate.timeutil import in_quiet_hours


def test_identical_payload_for_the_same_key_is_a_duplicate(setup):
    gate, _ = setup()
    assert gate.decide(ev("s", "k", T0, {"n": 1})).action == "wake"
    d = gate.decide(ev("s", "k", T0 + MIN, {"n": 1}))
    assert (d.action, d.reason) == ("skip", "duplicate")
    assert gate.decide(ev("s", "k", T0 + 2 * MIN, {"n": 2})).action == "wake"


def test_ignore_paths_stops_noisy_fields_from_counting_as_a_change(setup):
    gate, _ = setup("ignorePaths: [fetchedAt, meta.id]")
    gate.decide(ev("s", "k", T0, {"v": 1, "fetchedAt": 1, "meta": {"id": "a"}}))
    d = gate.decide(ev("s", "k", T0 + MIN, {"v": 1, "fetchedAt": 2, "meta": {"id": "b"}}))
    assert d.reason == "duplicate"


def test_different_keys_and_sources_are_tracked_independently(setup):
    gate, _ = setup()
    gate.decide(ev("a", "k", T0, 1))
    assert gate.decide(ev("b", "k", T0, 1)).action == "wake"
    assert gate.decide(ev("a", "other", T0, 1)).action == "wake"


def test_first_matching_rule_decides_default_otherwise(setup):
    gate, _ = setup(r"""
rules:
  - { name: bots, when: { path: data.actor, matches: "\\[bot\\]$" }, then: skip }
  - { name: docs, when: { path: data.kind, eq: docs }, then: batch }
default: wake
""")
    assert gate.decide(ev("s", "1", T0, {"actor": "ci[bot]"})).reason == "rule:bots"
    assert gate.decide(ev("s", "2", T0, {"kind": "docs"})).action == "batch"
    assert gate.decide(ev("s", "3", T0, {"kind": "bug"})).reason == "default"


def test_bypass_wakes_through_quiet_hours_cooldown_and_an_exhausted_budget(setup):
    gate, _ = setup("""
bypass:
  - { name: critical, when: { path: severity, eq: critical } }
cooldownSeconds: 3600
budget: { maxWakesPerDay: 0, quietHours: { start: "00:00", end: "23:59" }, timezone: UTC }
""")
    assert gate.decide(ev("s", "a", T0, 1)).action == "batch"
    d = gate.decide(ev("s", "a", T0 + MIN, 2, "critical"))
    assert (d.action, d.reason) == ("wake", "bypass:critical")


def test_cooldown_batches_changes_shortly_after_a_wake_then_allows_again(setup):
    gate, _ = setup("cooldownSeconds: 600")
    assert gate.decide(ev("s", "k", T0, 1)).action == "wake"
    assert gate.decide(ev("s", "k", T0 + 5 * MIN, 2)).reason == "cooldown"
    assert gate.decide(ev("s", "k", T0 + 11 * MIN, 3)).action == "wake"


def test_daily_budget_caps_wakes_resets_next_day_and_over_budget_can_skip(setup):
    gate, _ = setup("budget: { maxWakesPerDay: 2, timezone: UTC, overBudget: skip }")
    wake = lambda key, ts: gate.decide(ev("s", key, ts, key)).action  # noqa: E731
    assert wake("a", T0) == "wake"
    assert wake("b", T0 + MIN) == "wake"
    assert wake("c", T0 + 2 * MIN) == "skip"
    assert wake("d", T0 + 24 * 60 * MIN) == "wake"


def test_quiet_hours_wrap_midnight_and_use_the_configured_timezone():
    utc = lambda h, m=0: 1_767_571_200_000 + (h * 60 + m) * MIN  # 2026-01-05 00:00 UTC  # noqa: E731
    assert in_quiet_hours(utc(23), "22:00", "07:00", "UTC") is True
    assert in_quiet_hours(utc(3), "22:00", "07:00", "UTC") is True
    assert in_quiet_hours(utc(12), "22:00", "07:00", "UTC") is False
    # 23:00 UTC is 07:00 next day in Shanghai: just outside the window.
    assert in_quiet_hours(utc(23), "22:00", "07:00", "Asia/Shanghai") is False


def test_batched_events_are_returned_once_by_take_pending_then_cleared(setup):
    gate, store = setup("rules: [{ name: later, when: { path: source, eq: s }, then: batch }]")
    gate.decide(ev("s", "1", T0, 1))
    gate.decide(ev("s", "2", T0, 2))
    assert len(store.take_pending()) == 2
    assert len(store.take_pending()) == 0


def test_every_decision_including_skips_lands_in_the_audit_log_with_a_reason(setup):
    gate, store = setup()
    gate.decide(ev("s", "k", T0, 1))
    gate.decide(ev("s", "k", T0 + MIN, 1))
    assert [(r.action, r.reason) for r in store.recent_decisions(10)] == [("wake", "default"), ("skip", "duplicate")]


def test_invalid_config_fails_loudly_with_a_location():
    with pytest.raises(ValueError, match="name"):
        parse_config("rules: [{ when: { path: a, eq: 1 }, then: skip }]")
    with pytest.raises(ValueError, match="exactly one operator"):
        parse_config("rules: [{ name: x, when: { path: a, eq: 1, ne: 2 }, then: skip }]")
    with pytest.raises(ValueError, match="one of wake, skip, batch"):
        parse_config("rules: [{ name: x, when: { path: a, eq: 1 }, then: explode }]")
    with pytest.raises(ValueError, match="quietHours"):
        parse_config("budget: { quietHours: { start: '25:00', end: '07:00' } }")
    with pytest.raises(ValueError, match="unknown zone"):
        parse_config("budget: { timezone: Mars/Base }")


def test_replay_is_a_dry_run_and_reports_the_cost_difference():
    events = [ev("heartbeat", "hb", T0 + i * 30 * MIN, {"ok": True}) for i in range(48)]
    r = replay(events, parse_config("version: 1"), 1.4)
    assert r.counts["wake"] == 1
    assert r.counts["skip"] == 47
    assert abs(r.gated_cost - 1.4) < 1e-9
    assert abs(r.baseline_cost - 48 * 1.4) < 1e-9


REVIEW = """
rules:
  - { name: review, when: { path: data.state, eq: review_requested }, then: wake, exempt: [cooldown, budget] }
cooldownSeconds: 1800
budget: { maxWakesPerDay: 1, timezone: UTC }
"""


def test_an_exempt_rule_wakes_through_cooldown_and_an_exhausted_budget(setup):
    gate, _ = setup(REVIEW)
    assert gate.decide(ev("gh", "pr/1", T0, {"state": "opened"})).action == "wake"
    # Same key shortly after, and the daily budget (1) is already spent.
    d = gate.decide(ev("gh", "pr/1", T0 + 3 * MIN, {"state": "review_requested"}))
    assert (d.action, d.reason) == ("wake", "rule:review")
    # A non-exempt change on the same key is still held back.
    assert gate.decide(ev("gh", "pr/1", T0 + 6 * MIN, {"state": "commented"})).reason == "cooldown"


def test_exempt_does_not_cover_quiet_hours_unless_listed(setup):
    gate, _ = setup("""
rules:
  - { name: review, when: { path: data.state, eq: review_requested }, then: wake, exempt: [cooldown, budget] }
budget: { quietHours: { start: "00:00", end: "23:59" }, timezone: UTC }
""")
    assert gate.decide(ev("gh", "a", T0, {"state": "review_requested"})).reason == "quiet-hours"


def test_exempt_is_validated():
    with pytest.raises(ValueError, match="only rules with then: wake"):
        parse_config("rules: [{ name: x, when: { path: a, eq: 1 }, then: skip, exempt: [budget] }]")
    with pytest.raises(ValueError, match="cooldown, quietHours, budget"):
        parse_config("rules: [{ name: x, when: { path: a, eq: 1 }, then: wake, exempt: [nap] }]")
    with pytest.raises(ValueError, match="already ignore every limit"):
        parse_config("bypass: [{ name: x, when: { path: a, eq: 1 }, exempt: [budget] }]")


def test_flush_on_wake_hands_the_earlier_batch_over_once_on_the_next_wake(setup):
    gate, store = setup("cooldownSeconds: 600")
    gate.decide(ev("s", "k", T0, 1))  # wake
    assert gate.decide(ev("s", "k", T0 + MIN, 2)).action == "batch"
    nxt = gate.decide(ev("s", "other", T0 + 2 * MIN, 1))
    assert nxt.action == "wake"
    assert [b["key"] for b in nxt.batch] == ["k"]
    assert store.pending_count() == 0
    assert gate.decide(ev("s", "third", T0 + 3 * MIN, 1)).batch is None


def test_flush_on_wake_false_leaves_the_batch_for_an_explicit_flush(setup):
    gate, store = setup("flushOnWake: false\ncooldownSeconds: 600")
    gate.decide(ev("s", "k", T0, 1))
    gate.decide(ev("s", "k", T0 + MIN, 2))
    nxt = gate.decide(ev("s", "other", T0 + 2 * MIN, 1))
    assert nxt.batch is None
    assert store.pending_count() == 1


def test_replay_reports_batched_events_delivered_with_a_later_wake_and_those_stranded():
    events = [
        ev("s", "k", T0, 1),
        ev("s", "k", T0 + MIN, 2),  # batched by cooldown
        ev("s", "x", T0 + 2 * MIN, 1),  # wake, delivers it
        ev("s", "x", T0 + 3 * MIN, 2),  # batched, never followed by a wake
    ]
    r = replay(events, parse_config("cooldownSeconds: 600"))
    assert r.batch_delivered == 1
    assert r.batch_stranded == 1


def test_conditions_cover_every_operator(setup):
    gate, _ = setup("""
rules:
  - { name: n, when: { path: data.n, gte: 5 }, then: skip }
  - { name: tag, when: { path: data.tags, contains: urgent }, then: skip }
  - { name: has, when: { path: data.owner, exists: true }, then: skip }
  - { name: any-of, when: { any: [{ path: data.kind, in: [a, b] }, { not: { path: data.x, ne: 1 } }] }, then: skip }
""")
    reasons = [
        gate.decide(ev("s", str(i), T0, d)).reason
        for i, d in enumerate([{"n": 5}, {"tags": ["urgent"]}, {"owner": "x"}, {"kind": "b"}, {"x": 1}, {"n": 4}, {"flag": True}])
    ]
    assert reasons == ["rule:n", "rule:tag", "rule:has", "rule:any-of", "rule:any-of", "default", "default"]


def test_equality_is_strict_like_javascript(setup):
    gate, _ = setup("rules: [{ name: one, when: { path: data.v, eq: 1 }, then: skip }]")
    assert gate.decide(ev("s", "a", T0, {"v": 1})).reason == "rule:one"
    assert gate.decide(ev("s", "b", T0, {"v": True})).reason == "default"
    assert gate.decide(ev("s", "c", T0, {"v": "1"})).reason == "default"
