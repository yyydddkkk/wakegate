import io
import json
import sys
import time

from test_inspect import run_cli
from test_mcp import run_session

PY = sys.executable


def invoke(tmp_path, capsys, *argv):
    return run_cli(tmp_path, capsys, *argv)


def counter(tmp_path):
    """A command that appends a line to a file each time it runs, and prints what it is told."""
    path = tmp_path / "runs.txt"
    return path, ["--", PY, "-c", f"open({str(path)!r}, 'a').write('x\\n')"]


def runs(path):
    return len(path.read_text().splitlines()) if path.exists() else 0


def test_a_probe_turns_a_cheap_check_into_the_event_and_unchanged_means_no_run(tmp_path, capsys):
    path, cmd = counter(tmp_path)
    base = ["run", "--source", "inbox", "--key", "main", "--noop-empty"]
    code, out = invoke(tmp_path, capsys, *base, "--probe", "echo v1", *cmd)
    assert code == 0 and runs(path) == 1 and "outcome noop recorded" in out.err
    code, out = invoke(tmp_path, capsys, *base, "--probe", "echo v1", *cmd)
    assert runs(path) == 1 and "skip (duplicate)" in out.err, "same probe output: the agent is not started"
    code, out = invoke(tmp_path, capsys, *base, "--probe", "echo v2", *cmd)
    assert runs(path) == 2


def test_a_failing_probe_fails_open_so_the_agent_still_runs(tmp_path, capsys):
    path, cmd = counter(tmp_path)
    base = ["run", "--source", "inbox", "--key", "main", "--probe", "exit 3"]
    invoke(tmp_path, capsys, *base, *cmd)
    time.sleep(0.01)
    invoke(tmp_path, capsys, *base, *cmd)
    assert runs(path) == 2, "a broken check costs a wake, it does not hide one"


def test_the_event_can_come_from_stdin_and_the_command_then_gets_no_stdin(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"source": "gh", "key": "pr/1", "data": {"n": 1}})))
    path = tmp_path / "stdin.txt"
    code, out = invoke(tmp_path, capsys, "run", "--quiet", "--", PY, "-c", f"import sys; open({str(path)!r}, 'w').write(repr(sys.stdin.read()))")
    assert code == 0 and path.read_text() == "''"


def test_event_data_may_be_json_or_plain_text(tmp_path, capsys):
    path = tmp_path / "ev.json"
    invoke(tmp_path, capsys, "run", "--quiet", "--source", "s", "--key", "a", "--data", '{"n": 1}', "--",
           PY, "-c", f"import os; open({str(path)!r}, 'w').write(os.environ['WAKEGATE_EVENT'])")
    assert json.loads(path.read_text())["event"]["data"] == {"n": 1}
    invoke(tmp_path, capsys, "run", "--quiet", "--source", "s", "--key", "b", "--data", "just text", "--",
           PY, "-c", f"import os; open({str(path)!r}, 'w').write(os.environ['WAKEGATE_EVENT'])")
    assert json.loads(path.read_text())["event"]["data"] == "just text"


def test_the_wrapper_exits_with_the_commands_code_and_a_skip_exits_zero(tmp_path, capsys):
    code, _ = invoke(tmp_path, capsys, "run", "--quiet", "--source", "s", "--key", "a", "--data", "1", "--", PY, "-c", "raise SystemExit(4)")
    assert code == 4
    code, _ = invoke(tmp_path, capsys, "run", "--quiet", "--source", "s", "--key", "a", "--data", "1", "--skipped-exit", "9", "--", PY, "-c", "pass")
    assert code == 9, "the same event again is a duplicate, so the command is not run"


def test_usage_errors_are_clear(tmp_path, capsys):
    code, out = invoke(tmp_path, capsys, "run", "--source", "s", "--key", "a")
    assert code == 1 and "no command given" in out.err
    code, out = invoke(tmp_path, capsys, "run", "--source", "s", "--key", "a", "--noop-match", "(", "--", PY, "-c", "pass")
    assert code == 1 and "not a valid regex" in out.err
    code, out = invoke(tmp_path, capsys, "run", "--event", "nope", "--", PY, "-c", "pass")
    assert code == 1 and "not valid JSON" in out.err


def test_the_outcome_command_records_for_several_decisions_and_reports_errors(tmp_path, capsys):
    for i in range(2):
        invoke(tmp_path, capsys, "check", "--event", json.dumps({"source": "s", "key": f"k{i}", "data": i}))
    code, out = invoke(tmp_path, capsys, "outcome", "noop", "1", "2")
    assert code == 0 and json.loads(out.out) == {"outcome": "noop", "ids": [1, 2]}
    code, out = invoke(tmp_path, capsys, "outcome", "acted", "1", "99")
    assert code == 1 and "no decision with id 99" in out.err
    code, out = invoke(tmp_path, capsys, "report")
    assert "outcomes: 2 of 2 delivered wakes reported: noop 2" in out.out and "100% of the judged wakes found nothing to do" in out.out


def test_the_report_prints_the_shadow_check(tmp_path, capsys):
    cfg = tmp_path / "wakegate.yaml"
    cfg.write_text("rules: [{ name: nl, when: { path: data.kind, eq: newsletter }, then: skip }]\n")
    for i in range(4):
        invoke(tmp_path, capsys, "run", "--quiet", "--shadow", "--source", "mail", "--key", f"t{i}", "--data", json.dumps({"kind": "newsletter", "i": i}),
               "--noop-exit", "3", "--", PY, "-c", "raise SystemExit(3 if %d < 3 else 0)" % i)
    code, out = invoke(tmp_path, capsys, "report", "--shadow")
    assert "shadow check: 4 of 4 avoidable wakes have an outcome: 3 noop (confirmed avoidable), 1 acted; 25.0% would have been lost" in out.out


def test_mcp_outcome_tool():
    async def body(call, _):
        _, _, d = await call("wakegate_check", source="s", key="k", data=1)
        _, _, ok = await call("wakegate_outcome", outcome="noop", ids=[d["id"]])
        assert ok == {"outcome": "noop", "ids": [d["id"]]}
        _, _, r = await call("wakegate_report", since="1h")
        assert r["outcomes"]["noop"] == 1
        err, text, _ = await call("wakegate_outcome", outcome="noop", ids=[999])
        assert err is True and "no decision with id 999" in text
        err, _, _ = await call("wakegate_outcome", outcome="maybe", ids=[d["id"]])
        assert err is True
        err, _, _ = await call("wakegate_outcome", outcome="noop", ids=[])
        assert err is True

    run_session("version: 1", body)
