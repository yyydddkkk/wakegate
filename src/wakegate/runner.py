"""`wakegate run`: ask the gate, run the agent command only if it says wake, and report what it produced.

The point is to close the loop. The gate decides whether a run is worth it; this reports whether it was.
That observation (`acted` or `noop`) is the ground truth later layers learn from, and in shadow mode it
shows whether the wakes enforcing would have avoided really were empty.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .gate import Gate
from .store import Store, brief
from .timeutil import now_ms
from .types import Decision, WakeEvent

#: The tail of the command's output kept for --noop-match. Agents put their verdict at the end.
CAPTURE_BYTES = 256 * 1024
#: Above this the event is only available through the file, not the environment variable.
ENV_EVENT_LIMIT = 32 * 1024
TIMEOUT_EXIT = 124  # the same code GNU timeout uses


@dataclass
class NoopRules:
    """How to tell that a run found nothing to do. With no rule, the outcome is not recorded."""

    match: re.Pattern[str] | None = None
    empty: bool = False
    exit_code: int | None = None

    def any(self) -> bool:
        return self.match is not None or self.empty or self.exit_code is not None

    def needs_output(self) -> bool:
        return self.match is not None or self.empty


@dataclass
class RunResult:
    decision: Decision
    ran: bool
    #: The wrapper's exit code: the command's own when it ran, `skipped_exit` when it did not.
    exit_code: int
    #: acted, noop, error, or None when no rule was given or the command did not run.
    outcome: str | None
    recorded: list[int]
    seconds: float = 0.0


def classify(rules: NoopRules, exit_code: int, output: str | None) -> str | None:
    """Turn a finished run into an outcome. Pure, so the rules are easy to test."""
    if rules.exit_code is not None and exit_code == rules.exit_code:
        return "noop"
    if exit_code != 0:
        return "error"
    if not rules.any():
        return None
    if rules.empty and not (output or "").strip():
        return "noop"
    if rules.match is not None and output is not None and rules.match.search(output):
        return "noop"
    return "acted"


def _exit_code(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _write_out(data: bytes) -> None:
    out = getattr(sys.stdout, "buffer", None)
    if out is not None:
        out.write(data)
        out.flush()
    else:
        sys.stdout.write(data.decode("utf-8", errors="replace"))
        sys.stdout.flush()


def _pump(stream, tail: bytearray) -> None:
    for chunk in iter(lambda: stream.read1(65536), b""):
        _write_out(chunk)
        tail.extend(chunk)
        if len(tail) > CAPTURE_BYTES:
            del tail[: len(tail) - CAPTURE_BYTES]


def run_command(
    gate: Gate,
    store: Store,
    event: WakeEvent,
    argv: Sequence[str],
    *,
    rules: NoopRules | None = None,
    timeout: float | None = None,
    skipped_exit: int = 0,
    stdin_event: bool = False,
    stdin_inherit: bool = True,
    say: Callable[[str], None] = lambda s: None,
) -> RunResult:
    rules = rules or NoopRules()
    d = gate.decide(event)
    if d.action != "wake":
        say(f"wakegate: {d.action} ({d.reason}), decision #{d.id}; not running the command")
        return RunResult(d, False, skipped_exit, None, [])
    note = f" (rules said {d.verdict}, {d.mode} mode)" if d.verdict != d.action else ""
    carried = f", with {len(d.batch)} batched event(s)" if d.batch else ""
    say(f"wakegate: wake ({d.reason}){note}, decision #{d.id}{carried}")

    payload = {"decision": d.to_dict(), "event": event.to_dict(), "batch": d.batch or []}
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    fd, path = tempfile.mkstemp(prefix="wakegate-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        env = dict(os.environ)
        env.update(
            WAKEGATE_DECISION_ID=str(d.id), WAKEGATE_SOURCE=event.source, WAKEGATE_KEY=event.key,
            WAKEGATE_EVENT_FILE=path, WAKEGATE_BATCH_COUNT=str(len(d.batch or [])),
        )
        if len(text) <= ENV_EVENT_LIMIT:
            env["WAKEGATE_EVENT"] = text

        capture = rules.needs_output()
        stdin = subprocess.PIPE if stdin_event else (None if stdin_inherit else subprocess.DEVNULL)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(list(argv), stdin=stdin, stdout=subprocess.PIPE if capture else None, env=env)
        except OSError as e:
            say(f"wakegate: cannot run {argv[0]!r}: {e}")
            store.set_outcome([d.id], "error", now_ms(), "harness")
            return RunResult(d, True, 127, "error", [d.id])

        tail = bytearray()
        pump = None
        if capture:
            pump = threading.Thread(target=_pump, args=(proc.stdout, tail), daemon=True)
            pump.start()
        if stdin_event:
            try:
                proc.stdin.write(text.encode("utf-8"))
                proc.stdin.close()
            except BrokenPipeError:
                pass

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
            raise
        if pump is not None:
            pump.join(5)
        seconds = time.monotonic() - started

        if timed_out:
            code, outcome = TIMEOUT_EXIT, "error"
            say(f"wakegate: command timed out after {timeout}s")
        else:
            code = _exit_code(proc.returncode)
            outcome = classify(rules, code, tail.decode("utf-8", errors="replace") if capture else None)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    recorded: list[int] = []
    if outcome is None:
        say("wakegate: no outcome recorded (give --noop-match, --noop-empty or --noop-exit to tell empty runs apart)")
    else:
        ids = [d.id]
        # A noop is unambiguous for everything the run was shown. An acted is not: we cannot tell which
        # event of a batch caused the work, so only the event that woke the run is credited.
        if outcome == "noop":
            ids += [b["id"] for b in d.batch or []]
        store.set_outcome(ids, outcome, now_ms(), "harness")
        recorded = ids
        say(f"wakegate: outcome {outcome} recorded for decision(s) {', '.join('#' + str(i) for i in ids)}")
    return RunResult(d, True, code, outcome, recorded, seconds)
