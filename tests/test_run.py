import json
import re
import sys
import time

import pytest

from conftest import MIN, T0, ev
from jsonschema import Draft202012Validator
from pathlib import Path

from wakegate import Gate, Store, parse_config
from wakegate.queries import report
from wakegate.runner import NoopRules, classify, run_command
from wakegate.trace import build_trace

SCHEMA = json.loads((Path(__file__).parent.parent / "schemas" / "trace.v1.schema.json").read_text())
PY = sys.executable


def agent(code: str) -> list[str]:
    return [PY, "-c", code]


def setup_run(yaml="version: 1"):
    store = Store(":memory:")
    return Gate(parse_config(yaml), store), store


def run(gate, store, event, code, **kw):
    msgs: list[str] = []
    kw.setdefault("say", msgs.append)
    r = run_command(gate, store, event, agent(code), **kw)
    return r, msgs


def test_classify_is_a_pure_function_of_exit_code_and_output():
    rules = NoopRules(match=re.compile("HEARTBEAT_OK"), empty=True, exit_code=3)
    assert classify(rules, 3, "work done") == "noop", "the noop exit code wins"
    assert classify(rules, 1, "") == "error"
    assert classify(rules, 0, "   \n") == "noop"
    assert classify(rules, 0, "all fine: HEARTBEAT_OK") == "noop"
    assert classify(rules, 0, "opened PR 12") == "acted"
    assert classify(NoopRules(), 0, None) is None, "no rule: do not guess"
    assert classify(NoopRules(), 2, None) == "error"
    assert classify(NoopRules(match=re.compile("x")), 0, "y") == "acted"


def test_a_skip_does_not_run_the_command_and_exits_with_the_skipped_code(tmp_path):
    gate, store = setup_run()
    marker = tmp_path / "ran"
    code = f"open({str(marker)!r}, 'w').write('x')"
    run(gate, store, ev("s", "k", T0, 1), code)
    assert marker.exists()
    marker.unlink()
    r, msgs = run(gate, store, ev("s", "k", T0 + MIN, 1), code, skipped_exit=7)
    assert not marker.exists() and (r.ran, r.exit_code, r.outcome) == (False, 7, None)
    assert "skip (duplicate)" in msgs[0] and "not running" in msgs[0]


def test_a_wake_runs_the_command_gives_it_the_event_and_returns_its_exit_code(tmp_path):
    gate, store = setup_run()
    out = tmp_path / "seen.json"
    code = (
        "import os, json; "
        f"json.dump({{'id': os.environ['WAKEGATE_DECISION_ID'], 'key': os.environ['WAKEGATE_KEY'], 'env': json.loads(os.environ['WAKEGATE_EVENT']), "
        "'file': json.load(open(os.environ['WAKEGATE_EVENT_FILE'])), 'path': os.environ['WAKEGATE_EVENT_FILE']}, "
        f"open({str(out)!r}, 'w')); raise SystemExit(5)"
    )
    r, _ = run(gate, store, ev("gh", "pr/1", T0, {"state": "open"}), code)
    seen = json.loads(out.read_text())
    assert r.exit_code == 5 and r.ran
    assert seen["id"] == str(r.decision.id) and seen["key"] == "pr/1"
    assert seen["env"]["event"]["data"] == {"state": "open"} and seen["file"] == seen["env"]
    assert not Path(seen["path"]).exists(), "the temporary event file is removed afterwards"


@pytest.mark.parametrize("code, rules, expected", [
    ("pass", NoopRules(empty=True), "noop"),
    ("print('did the thing')", NoopRules(empty=True), "acted"),
    ("print('all quiet HEARTBEAT_OK')", NoopRules(match=re.compile("HEARTBEAT_OK")), "noop"),
    ("print('opened a PR')", NoopRules(match=re.compile("HEARTBEAT_OK")), "acted"),
    ("raise SystemExit(3)", NoopRules(exit_code=3), "noop"),
    ("raise SystemExit(1)", NoopRules(empty=True), "error"),
    ("print('x')", NoopRules(), None),
])
def test_the_outcome_is_recorded_from_the_run(code, rules, expected, capfd):
    gate, store = setup_run()
    r, msgs = run(gate, store, ev("s", "k", T0, 1), code, rules=rules)
    assert r.outcome == expected
    got = store.outcomes().get(r.decision.id)
    assert (got["outcome"] if got else None) == expected
    if expected is None:
        assert any("no outcome recorded" in m for m in msgs)


def test_the_commands_output_passes_through_while_it_is_captured(capfd):
    gate, store = setup_run()
    r, _ = run(gate, store, ev("s", "k", T0, 1), "print('visible to the caller HEARTBEAT_OK')", rules=NoopRules(match=re.compile("HEARTBEAT_OK")))
    assert "visible to the caller" in capfd.readouterr().out and r.outcome == "noop"


def test_a_timeout_stops_the_command_and_records_an_error():
    gate, store = setup_run()
    started = time.monotonic()
    r, msgs = run(gate, store, ev("s", "k", T0, 1), "import time; time.sleep(30)", timeout=0.4)
    assert time.monotonic() - started < 10
    assert (r.exit_code, r.outcome) == (124, "error") and any("timed out" in m for m in msgs)


def test_a_command_that_cannot_start_is_an_error_not_a_crash():
    gate, store = setup_run()
    r = run_command(gate, store, ev("s", "k", T0, 1), ["/no/such/binary"], say=lambda s: None)
    assert (r.exit_code, r.outcome) == (127, "error")


def test_a_noop_covers_the_batch_it_was_shown_but_acted_only_credits_the_waking_event():
    yaml = "cooldownSeconds: 600"
    for code, expect_ids in (("pass", 2), ("print('did it')", 1)):
        gate, store = setup_run(yaml)
        gate.decide(ev("s", "k", T0, 1))                         # wake (cooldown starts)
        batched = gate.decide(ev("s", "k", T0 + MIN, 2))          # held back
        assert batched.action == "batch"
        r, _ = run(gate, store, ev("s", "other", T0 + 2 * MIN, 1), code, rules=NoopRules(empty=True))
        assert [b["id"] for b in r.decision.batch] == [batched.id]
        assert len(r.recorded) == expect_ids, f"{code}: recorded {r.recorded}"
        assert (batched.id in store.outcomes()) == (expect_ids == 2)


def test_stdin_event_gives_the_command_the_payload():
    gate, store = setup_run()
    code = "import sys, json; d = json.load(sys.stdin); print(d['event']['key'], d['decision']['id'])"
    r, _ = run(gate, store, ev("s", "pr/9", T0, 1), code, stdin_event=True, rules=NoopRules(empty=True))
    assert r.outcome == "acted"


def test_in_shadow_mode_the_command_always_runs_and_outcomes_check_the_would_be_skips(capfd):
    gate, store = setup_run("mode: shadow\nrules: [{ name: noise, when: { path: data.kind, eq: noise }, then: skip }]")
    rules = NoopRules(match=re.compile("nothing"))
    for i in range(6):
        r, _ = run(gate, store, ev("s", f"n{i}", T0 + i * MIN, {"kind": "noise", "i": i}), "print('nothing to do')" if i < 5 else "print('found a real problem')", rules=rules)
        assert r.ran and r.decision.verdict == "skip"
    ac = report(store, T0 - MIN)["avoidanceCheck"]
    assert (ac["avoidable"], ac["withOutcome"], ac["noop"], ac["acted"]) == (6, 6, 5, 1)
    assert abs(ac["wouldHaveLostRate"] - 1 / 6) < 1e-9 and ac["ci95"][0] < 1 / 6 < ac["ci95"][1]


def test_report_summarises_outcomes_of_delivered_wakes():
    gate, store = setup_run()
    for i, code in enumerate(["pass", "pass", "print('x')", "raise SystemExit(2)"]):
        run(gate, store, ev("s", f"k{i}", T0 + i * MIN, i), code, rules=NoopRules(empty=True))
    gate.decide(ev("s", "unreported", T0 + 9 * MIN, 9))
    o = report(store, T0 - MIN)["outcomes"]
    assert (o["noop"], o["acted"], o["error"]) == (2, 1, 1)
    assert (o["wakesDelivered"], o["wakesWithOutcome"]) == (5, 4)
    assert abs(o["noopShare"] - 2 / 3) < 1e-9, "errors are not counted as a judgement"


def test_the_outcome_appears_in_the_trace_and_the_trace_still_validates():
    gate, store = setup_run()
    r, _ = run(gate, store, ev("s", "k", T0, 1), "pass", rules=NoopRules(empty=True))
    t = build_trace(store, r.decision.id)
    Draft202012Validator(SCHEMA).validate(t)
    assert t["outcome"]["outcome"] == "noop" and t["outcome"]["source"] == "harness"
    other = gate.decide(ev("s", "k2", T0, 2))
    assert build_trace(store, other.id)["outcome"] is None


def test_set_outcome_validates():
    gate, store = setup_run()
    d = gate.decide(ev("s", "k", T0, 1))
    with pytest.raises(ValueError, match="no decision with id 99"):
        store.set_outcome([d.id, 99], "noop", T0, "x")
    with pytest.raises(ValueError, match="acted"):
        store.set_outcome([d.id], "maybe", T0, "x")
    store.set_outcome([d.id], "noop", T0, "x")
    store.set_outcome([d.id], "acted", T0 + 1, "y")
    assert store.outcomes()[d.id]["outcome"] == "acted", "the latest report wins"
