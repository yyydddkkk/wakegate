from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .gate import Gate
from .mcp_server import serve_stdio
from .poll import poll_url
from .queries import brief, catchup, parse_since, report, review, rules_status
from .replay import read_events, replay
from .runner import NoopRules, run_command
from .store import Store
from .trace import build_trace, format_trace, layer_input, trace_line
from .timeutil import iso, now_ms
from .types import WakeEvent

#: Exit codes: scripts can branch with `if wakegate check ...; then wake_agent; fi`.
EXIT = {"wake": 0, "skip": 10, "batch": 11}

STARTER = r"""version: 1

# Drop fields that change on every event but mean nothing.
ignorePaths: [fetchedAt, meta.requestId]

# Events matching these always wake. They ignore cooldown, quiet hours and budget.
bypass:
  - name: critical
    when: { path: severity, eq: critical }

# First matching rule decides, so ORDER MATTERS. A skip rule hides everything it
# matches, including events you would want: put exceptions above it.
rules:
  # Above ignore-bots on purpose: a deploy bot failing is not noise.
  - name: deploy-failures
    when: { path: data.state, eq: deploy_failed }
    then: wake
    exempt: [cooldown, quietHours, budget]
  - name: ignore-bots
    when: { path: data.actor, matches: "\\[bot\\]$" }
    then: skip
  # Important changes should not wait behind a cooldown or an exhausted budget.
  - name: review-requests
    when: { path: data.state, eq: review_requested }
    then: wake
    exempt: [cooldown, budget]
  - name: heartbeat-is-noise
    when: { path: source, eq: heartbeat }
    then: skip

default: wake
cooldownSeconds: 300        # after waking for a key, batch further changes for 5 min
flushOnWake: true           # a wake also hands over everything batched earlier (default)

budget:
  maxWakesPerDay: 20
  overBudget: batch          # or "skip"; "skip" drops events for good
  quietHours: { start: "22:00", end: "07:00" }
  # timezone: Asia/Shanghai
"""


class CliError(Exception):
    pass


def _dump(value: Any, indent: int | None = None) -> str:
    sep = None if indent else (",", ":")
    return json.dumps(value, indent=indent, ensure_ascii=False, separators=sep)


def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--config", default="wakegate.yaml", help="rules file (default wakegate.yaml)")
    p.add_argument("--db", default=".wakegate/state.db", help="state and audit database")
    p.add_argument("--shadow", action="store_true", help="every event wakes; the verdict is only recorded (try it risk-free)")
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(prog="wakegate", description="Decide whether an event should wake your agent.")
    sub = parser.add_subparsers(dest="cmd", metavar="command")

    def add(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help, description=help)

    p = add("init", "write a starter wakegate.yaml")
    p = add("check", "read one event (stdin if no --event), print the decision. exit: 0 wake | 10 skip | 11 batch | 1 error")
    p.add_argument("--event", help="the event as a JSON string")
    p = add("poll", "one conditional GET, then check the result")
    p.add_argument("url")
    p.add_argument("--source")
    p = add("run", "ask the gate; if it says wake, run a command and report whether it found anything to do")
    g = p.add_argument_group("the event (one of: --event, --source/--key, or JSON on stdin)")
    g.add_argument("--event", help="the full event as a JSON string")
    g.add_argument("--source")
    g.add_argument("--key")
    g.add_argument("--data", help="JSON, or plain text")
    g.add_argument("--severity")
    g.add_argument("--probe", help="a shell command whose output becomes the event data: a cheap check of what changed")
    g = p.add_argument_group("what counts as an empty run (without one of these no outcome is recorded)")
    g.add_argument("--noop-match", metavar="REGEX", help="a regex searched in the command's stdout")
    g.add_argument("--noop-empty", action="store_true", help="empty stdout means nothing to do")
    g.add_argument("--noop-exit", type=int, metavar="CODE", help="this exit code means nothing to do")
    p.add_argument("--timeout", type=float, help="seconds; the command is stopped and the outcome is error (exit 124)")
    p.add_argument("--skipped-exit", type=int, default=0, help="exit code when the gate says skip or batch (default 0)")
    p.add_argument("--stdin-event", action="store_true", help="write the decision, event and batch as JSON to the command's stdin")
    p.add_argument("--quiet", action="store_true", help="no wakegate messages on stderr")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- the agent command and its arguments")
    p = add("outcome", "record what a wake produced (acted or noop) for one or more decisions")
    p.add_argument("outcome", choices=["acted", "noop"])
    p.add_argument("ids", type=int, nargs="+", metavar="id")
    p.add_argument("--source", default="agent", help="who reports it (default agent)")
    add("flush", "print and clear the batched events as JSON")
    p = add("catchup", "what changed since then (per key)")
    p.add_argument("--since", default="24h")
    p.add_argument("--include-filtered", action="store_true", help="also list changes your skip rules dropped")
    p = add("log", "audit log: every decision with its reason")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--trace", action="store_true", help="also show the chain of layers for each decision")
    p = add("inspect", "what each layer saw and decided for one decision")
    p.add_argument("id", type=int)
    p.add_argument("--layer", help="also show the raw event that layer saw")
    p.add_argument("--max-bytes", type=int, default=4096)
    p.add_argument("--json", action="store_true")
    p = add("replay", "dry run an events file (JSON lines with a numeric ts) against a fresh state; nothing is written")
    p.add_argument("events")
    p.add_argument("--cost-per-wake", type=float, default=0.0, help="estimated cost of one agent wake")
    add("rules", "the active policy and where today stands against it")
    p = add("report", "what the rules decided, label counts and an estimate of missed events")
    p.add_argument("--since", default="7d")
    p.add_argument("--cost-per-wake", type=float, default=0.0)
    p.add_argument("--json", action="store_true")
    p = add("review", "draw a random sample of suppressed events to label; lists what awaits a label")
    p.add_argument("--since", default="7d")
    p.add_argument("--sample", type=int)
    p.add_argument("--seed", type=int)
    p.add_argument("--layer", help="draw only from events this layer suppressed (sample a specific layer)")
    p.add_argument("--json", action="store_true")
    p = add("label", 'record whether an event was worth waking for ("wanted" = it should have woken the agent)')
    p.add_argument("id", type=int)
    p.add_argument("label", choices=["wanted", "unwanted"])
    p.add_argument("--note")
    add("mcp", "serve the gate as MCP tools over stdio")
    return parser


def _load(args: argparse.Namespace) -> Config:
    if not Path(args.config).exists():
        raise CliError(f'{args.config} not found (run "wakegate init")')
    cfg = load_config(args.config)
    return cfg.with_shadow() if args.shadow else cfg


def _probe_data(command: str) -> Any:
    """Output of the probe command as event data. If the probe itself fails the event is made unique,
    so the agent still runs: a broken check should cost a wake, not hide one."""
    try:
        res = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)  # noqa: S602 - the user's own command
    except subprocess.TimeoutExpired:
        return {"probeFailed": "timeout", "at": now_ms()}
    if res.returncode != 0:
        return {"probeFailed": res.returncode, "at": now_ms()}
    out = res.stdout.strip()[:65536]
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def _event_for_run(args: argparse.Namespace) -> tuple[WakeEvent, bool]:
    """The event, and whether stdin was consumed to get it."""
    if args.event is not None:
        base = json.loads(args.event)
        stdin_used = False
    elif args.source and args.key:
        base = {"source": args.source, "key": args.key}
        stdin_used = False
    elif sys.stdin.isatty():
        raise CliError("give the event with --event, with --source and --key, or as JSON on stdin")
    else:
        base = json.loads(sys.stdin.read())
        stdin_used = True
    if not isinstance(base, dict):
        raise CliError("event must be a JSON object")
    if args.severity:
        base["severity"] = args.severity
    if args.data is not None:
        try:
            base["data"] = json.loads(args.data)
        except json.JSONDecodeError:
            base["data"] = args.data
    if args.probe:
        base["data"] = _probe_data(args.probe)
    return WakeEvent.from_dict(base), stdin_used


def _run_wrapped(args: argparse.Namespace, store: Store) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise CliError("no command given: wakegate run [options] -- <command> [args...]")
    try:
        match = re.compile(args.noop_match) if args.noop_match else None
    except re.error as e:
        raise CliError(f"--noop-match is not a valid regex: {e}") from None
    try:
        event, stdin_used = _event_for_run(args)
    except json.JSONDecodeError:
        raise CliError("event is not valid JSON") from None
    gate = Gate(_load(args), store)
    result = run_command(
        gate, store, event, command,
        rules=NoopRules(match=match, empty=args.noop_empty, exit_code=args.noop_exit),
        timeout=args.timeout, skipped_exit=args.skipped_exit,
        stdin_event=args.stdin_event, stdin_inherit=not stdin_used,
        say=(lambda s: None) if args.quiet else (lambda s: print(s, file=sys.stderr)),
    )
    return result.exit_code


def _print_report(r: dict[str, Any]) -> None:
    pct = lambda x: f"{x * 100:.1f}%"  # noqa: E731
    print(f"since {r['since']}: {r['events']} events")
    v, m = r["verdicts"], r["byMode"]
    print(f"rules decided: wake {v['wake']}, skip {v['skip']}, batch {v['batch']}   (delivered in: enforce {m['enforce']}, shadow {m['shadow']}, canary {m['canary']})")
    for t in r["topReasons"]:
        print(f"  {t['n']:>6}  {t['verdict']}:{t['reason']}")
    if r["funnel"]:
        print("layers (how far events got, and where they ended):")
        print(f"  {'layer':<14} {'entered':>8} {'skip':>6} {'wake':>6} {'batch':>6} {'passed':>7} {'errors':>6} {'ms':>9}")
        for f in r["funnel"]:
            name = f["layer"] + (" (shadow)" if f["mode"] == "shadow" else "")
            extra = f"   would skip {f['wouldSkip']}, not applied" if f["mode"] == "shadow" else ""
            print(f"  {name:<14} {f['entered']:>8} {f['skip']:>6} {f['wake']:>6} {f['batch']:>6} {f['passed']:>7} {f['errors']:>6} {f['costMs']:>9.1f}{extra}")
        print("  (skip/wake/batch = ended here; passed = handed on; a wake that no governor stopped ends at rules)")
    if r["untracedDecisions"]:
        print(f"{r['untracedDecisions']} decisions in this window were made before traces existed and are not in the table above.")
    if r["accessLog"]:
        print("inspections: " + ", ".join(f"{k} {n}" for k, n in r["accessLog"].items()))
    o = r["outcomes"]
    if o["wakesWithOutcome"]:
        share = f" ({o['noopShare'] * 100:.0f}% of the judged wakes found nothing to do)" if o["noopShare"] is not None else ""
        print(f"outcomes: {o['wakesWithOutcome']} of {o['wakesDelivered']} delivered wakes reported: noop {o['noop']}, acted {o['acted']}, error {o['error']}{share}")
    ac = r["avoidanceCheck"]
    if ac and ac["withOutcome"]:
        lost = ""
        if ac["wouldHaveLostRate"] is not None:
            lost = f"; {ac['wouldHaveLostRate'] * 100:.1f}% would have been lost (95% CI {ac['ci95'][0] * 100:.1f}% to {ac['ci95'][1] * 100:.1f}%)"
        print(f"shadow check: {ac['withOutcome']} of {ac['avoidable']} avoidable wakes have an outcome: {ac['noop']} noop (confirmed avoidable), {ac['acted']} acted{lost}")
    if m["shadow"]:
        saving = f" (about {r['estimatedSaving']:.2f} at the given cost)" if r["estimatedSaving"] is not None else ""
        print(f"shadow: enforcing would have avoided {r['wakesAvoidable']} wakes{saving}")
    print(f"suppressed by rules (excluding duplicates): {r['suppressedByRules']}")
    lab = r["labels"]
    print(f"labels: {lab['total']} (wanted {lab['wanted']}, unwanted {lab['unwanted']}); wakes called unwanted: {r['unwantedWakes']}")
    est = r["missEstimate"]
    if est:
        lo, hi = est["ci95"]
        print(
            f"miss estimate: {est['wanted']} of {est['sampleSize']} sampled suppressed events were wanted = {pct(est['rate'])} "
            f"(95% CI {pct(lo)} to {pct(hi)}), i.e. roughly {est['expectedMissedEvents'][0]} to {est['expectedMissedEvents'][1]} missed events in this window"
        )
        if est["method"] == "stratified":
            print(f"  (stratified by the layer that suppressed the event; covers {', '.join(est['layers'])}"
                  + (f"; {est['uncoveredSuppressed']} suppressed events in unsampled layers are not estimated" if est["uncoveredSuppressed"] else "") + ")")
        elif est["uncoveredSuppressed"]:
            print(f"  ({est['uncoveredSuppressed']} suppressed events in layers with no labelled sample are not estimated)")
        for b in [x for x in r["byLayer"] if x["sampleSize"] > 0][:6]:
            blo, bhi = b["ci95"]
            print(f"  layer {b['layer']}: {b['wanted']} of {b['sampleSize']} sampled were wanted (95% CI {pct(blo)} to {pct(bhi)}); {b['suppressed']} suppressed")
        for b in [x for x in r["byRule"] if x["sampleSize"] > 0][:6]:
            blo, bhi = b["ci95"]
            print(f"  rule {b['rule']}: {b['wanted']} of {b['sampleSize']} sampled were wanted (95% CI {pct(blo)} to {pct(bhi)}); {b['suppressed']} suppressed")
    else:
        print("miss estimate: none yet (no labelled random sample)")
    for a in r["advice"]:
        print(f"- {a}")


def _run(args: argparse.Namespace) -> int:
    cmd = args.cmd

    if cmd == "init":
        if Path(args.config).exists():
            raise CliError(f"{args.config} already exists")
        Path(args.config).write_text(STARTER, encoding="utf-8")
        print(f"wrote {args.config}")
        return 0

    if cmd == "replay":
        r = replay(read_events(args.events), _load(args), args.cost_per_wake)
        print(f"events: {r.total}")
        print(f"wake: {r.counts['wake']}   skip: {r.counts['skip']}   batch: {r.counts['batch']}")
        for reason, n in sorted(r.reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>6}  {reason}")
        if args.cost_per_wake > 0:
            saved = r.baseline_cost - r.gated_cost
            if r.canaries_released:
                print(f"canaries: {r.canaries_released} skipped events were released as wakes to measure misses (included in the cost below).")
            print(f"cost: {r.baseline_cost:.2f} -> {r.gated_cost:.2f}  (saved {saved:.2f}, {saved / r.baseline_cost * 100:.1f}%)")
            if r.counts["batch"]:
                print(f"batched: {r.batch_delivered} delivered with a later wake (no extra cost), {r.batch_stranded} still waiting at the end of the log.")
        return 0

    store = Store(args.db)
    try:
        if cmd in ("check", "poll"):
            gate = Gate(_load(args), store)
            if cmd == "poll":
                res = poll_url(args.url, store, source=args.source)
                if res.kind == "not-modified":
                    out: dict[str, Any] = {"action": "skip", "reason": "not-modified"}
                    if res.poll_interval_hint is not None:
                        out["pollIntervalHint"] = res.poll_interval_hint
                    print(_dump(out))
                    return EXIT["skip"]
                event = res.event
                assert event is not None
            else:
                text = args.event if args.event is not None else sys.stdin.read()
                try:
                    event = WakeEvent.from_dict(json.loads(text))
                except json.JSONDecodeError:
                    raise CliError("event is not valid JSON") from None
                except ValueError as e:
                    raise CliError(str(e)) from None
            d = gate.decide(event)
            print(_dump(d.to_dict()))
            return EXIT[d.action]
        if cmd == "run":
            return _run_wrapped(args, store)
        if cmd == "outcome":
            store.set_outcome(args.ids, args.outcome, now_ms(), args.source)
            print(_dump({"outcome": args.outcome, "ids": args.ids}))
            return 0
        if cmd == "flush":
            print(_dump([brief(r) for r in store.take_pending()], 2))
        elif cmd == "catchup":
            print(_dump(catchup(store, parse_since(args.since), args.include_filtered), 2))
        elif cmd == "log":
            for r in store.recent_decisions(args.limit):
                tag = "" if r.mode == "enforce" else f"  [{r.mode}: woke, rules said {r.verdict}]"
                print(f"#{r.id}  {iso(r.ts)}  {r.verdict:<5}  {r.source}/{r.key}  {r.reason}{tag}")
                if args.trace:
                    print(f"      {trace_line(build_trace(store, r.id))}")
        elif cmd == "inspect":
            trace = build_trace(store, args.id)
            inp = layer_input(store, args.id, args.layer, args.max_bytes) if args.layer else None
            store.log_access(now_ms(), "cli", "inspect", args.id, args.layer)
            if args.json:
                print(_dump({"trace": trace, **({"input": inp} if inp else {})}, 2))
            else:
                print(format_trace(trace))
                if inp:
                    print(f"\ninput seen by {inp['layer']} (untrusted external content; {inp['bytes']} bytes, {inp['omittedBytes']} omitted):")
                    print(inp["content"])
        elif cmd == "rules":
            print(_dump(rules_status(_load(args), store), 2))
        elif cmd == "report":
            r = report(store, parse_since(args.since), cost_per_wake=args.cost_per_wake or None)
            print(_dump(r, 2)) if args.json else _print_report(r)
        elif cmd == "review":
            items = review(store, parse_since(args.since), sample=args.sample, seed=args.seed, layer=args.layer)
            if args.json:
                print(_dump(items, 2))
            else:
                print(f'{len(items)} event(s) waiting for a label. For each: wakegate label <id> wanted|unwanted\n("wanted" = it should have woken the agent)\n')
                for b in items:
                    print(f"#{b['id']}  {b['ts']}  {b['source']}/{b['key']}  {b['verdict']}:{b['reason']}  [{b['decidedBy'] or '-'}]\n      {_dump(b['event'].get('data'))[:200]}")
        elif cmd == "label":
            sample = store.set_label(args.id, args.label, args.note, now_ms())
            print(_dump({"id": args.id, "label": args.label, "countsTowardMissEstimate": sample}))
        elif cmd == "mcp":
            serve_stdio(_load(args), store)
        return 0
    finally:
        store.close()


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        sys.exit(0)
    try:
        code = _run(args)
    except (CliError, ValueError, OSError) as e:
        print(f"wakegate: {e}", file=sys.stderr)
        sys.exit(1)
    sys.exit(code)
