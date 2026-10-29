import json

import pytest

from conftest import MIN, T0, ev
from wakegate import cli
from wakegate.queries import report
from wakegate.trace import build_trace, format_trace, layer_input, trace_line


def test_the_funnel_counts_where_events_end_and_what_was_handed_on(setup):
    gate, store = setup("""
rules: [{ name: nl, when: { path: data.kind, eq: newsletter }, then: skip }]
budget: { maxWakesPerDay: 2, timezone: UTC }
""")
    gate.decide(ev("s", "a", T0, {"n": 1}))                      # wake
    gate.decide(ev("s", "b", T0 + MIN, {"n": 2}))                # wake
    gate.decide(ev("s", "c", T0 + 2 * MIN, {"n": 3}))            # over budget -> batch
    gate.decide(ev("s", "d", T0 + 3 * MIN, {"kind": "newsletter"}))  # skipped by a rule
    gate.decide(ev("s", "a", T0 + 4 * MIN, {"n": 1}))            # duplicate
    f = {x["layer"]: x for x in report(store, T0 - MIN)["funnel"]}

    assert [x["layer"] for x in report(store, T0 - MIN)["funnel"]] == ["dedupe", "rules", "budget"]
    assert (f["dedupe"]["entered"], f["dedupe"]["skip"], f["dedupe"]["passed"]) == (5, 1, 4)
    # rules: 4 entered; 2 wakes ended here (no governor stopped them), 1 skip, 1 proposal handed on and then batched
    assert (f["rules"]["entered"], f["rules"]["wake"], f["rules"]["skip"], f["rules"]["passed"]) == (4, 2, 1, 1)
    # budget: saw the 3 wake proposals, stopped one
    assert (f["budget"]["entered"], f["budget"]["batch"], f["budget"]["passed"]) == (3, 1, 2)
    assert all(x["costMs"] >= 0 and x["errors"] == 0 for x in f.values())


def test_decisions_made_before_traces_are_counted_but_kept_out_of_the_funnel(setup):
    gate, store = setup("version: 1")
    store._db.execute(
        "INSERT INTO decisions (ts, day, source, key, fingerprint, action, verdict, mode, reason, rule, changed, event_json)"
        " VALUES (?, '2026-01-05', 's', 'old', 'f', 'wake', 'wake', 'enforce', 'default', NULL, 1, '{\"source\":\"s\",\"key\":\"old\"}')",
        (T0,),
    )
    gate.decide(ev("s", "new", T0 + MIN, 1))
    r = report(store, T0 - MIN)
    assert r["untracedDecisions"] == 1
    assert r["funnel"][0]["entered"] == 1


def test_layer_input_is_untrusted_capped_and_lists_what_came_before(setup):
    gate, store = setup("version: 1")
    d = gate.decide(ev("s", "k", T0, {"text": "x" * 5000}))
    full = layer_input(store, d.id, "rules", max_bytes=16384)
    assert full["untrusted"] is True and full["omittedBytes"] == 0
    assert [u["layer"] for u in full["upstream"]] == ["dedupe"]

    capped = layer_input(store, d.id, "rules", max_bytes=1000)
    assert len(capped["content"].encode()) <= 1000
    assert capped["omittedBytes"] == capped["bytes"] - len(capped["content"].encode())
    assert capped["omittedBytes"] > 4000
    # the cap cannot be raised past the hard limit
    assert layer_input(store, d.id, "rules", max_bytes=10**9)["omittedBytes"] == 0

    with pytest.raises(ValueError, match="layers that ran: dedupe, rules"):
        layer_input(store, d.id, "classifier")


def test_layer_input_never_splits_a_multibyte_character(setup):
    gate, store = setup("version: 1")
    d = gate.decide(ev("s", "k", T0, {"t": "唤醒" * 600}))
    out = layer_input(store, d.id, "rules", max_bytes=300)
    out["content"].encode("utf-8")  # valid text, not cut mid-character
    assert out["bytes"] > 300


def test_trace_text_is_readable(setup):
    gate, store = setup("""
rules:
  - { name: bots, when: { path: data.actor, matches: "\\\\[bot\\\\]$" }, then: skip }
cooldownSeconds: 60
""")
    d = gate.decide(ev("s", "k", T0, {"actor": "ci[bot]"}))
    t = build_trace(store, d.id)
    assert trace_line(t) == "dedupe:defer > rules:skip(rule:bots)"
    text = format_trace(t)
    assert "decision #" in text and "rules" in text and "final" in text
    assert "result: skip by rules (rule:bots)" in text


def run_cli(tmp_path, capsys, *argv):
    cfg = tmp_path / "wakegate.yaml"
    if not cfg.exists():
        cfg.write_text('rules: [{ name: nl, when: { path: data.kind, eq: newsletter }, then: skip }]\n')
    code = 0
    try:
        cli.main([*argv[:1], "--config", str(cfg), "--db", str(tmp_path / "s.db"), *argv[1:]])
    except SystemExit as e:
        code = e.code or 0
    return code, capsys.readouterr()


def test_cli_inspect_log_trace_and_report_funnel(tmp_path, capsys):
    run_cli(tmp_path, capsys, "check", "--event", json.dumps({"source": "mail", "key": "t1", "data": {"kind": "newsletter", "body": "hello"}}))
    run_cli(tmp_path, capsys, "check", "--event", json.dumps({"source": "gh", "key": "pr1", "data": {"n": 1}}))

    code, out = run_cli(tmp_path, capsys, "log", "--trace")
    assert "dedupe:defer > rules:skip(rule:nl)" in out.out and "dedupe:defer > rules:wake(default)" in out.out

    code, out = run_cli(tmp_path, capsys, "inspect", "1")
    assert code == 0 and "decision #1" in out.out and "result: skip by rules" in out.out and "hello" not in out.out

    code, out = run_cli(tmp_path, capsys, "inspect", "1", "--layer", "rules")
    assert "untrusted external content" in out.out and "hello" in out.out

    code, out = run_cli(tmp_path, capsys, "inspect", "1", "--json")
    assert json.loads(out.out)["trace"]["trace_version"] == 1

    code, out = run_cli(tmp_path, capsys, "inspect", "99")
    assert code == 1 and "no decision with id 99" in out.err

    code, out = run_cli(tmp_path, capsys, "report")
    assert "layers (how far events got" in out.out and "dedupe" in out.out and "rules" in out.out
    assert "inspections: inspect 3" in out.out  # the three successful inspects above
