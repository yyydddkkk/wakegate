from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .gate import Gate
from .mcp_server import serve_stdio
from .poll import poll_url
from .queries import brief, catchup, parse_since, report, review, rules_status
from .replay import read_events, replay
from .store import Store
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
    add("flush", "print and clear the batched events as JSON")
    p = add("catchup", "what changed since then (per key)")
    p.add_argument("--since", default="24h")
    p.add_argument("--include-filtered", action="store_true", help="also list changes your skip rules dropped")
    p = add("log", "audit log: every decision with its reason")
    p.add_argument("--limit", type=int, default=50)
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


def _print_report(r: dict[str, Any]) -> None:
    pct = lambda x: f"{x * 100:.1f}%"  # noqa: E731
    print(f"since {r['since']}: {r['events']} events")
    v, m = r["verdicts"], r["byMode"]
    print(f"rules decided: wake {v['wake']}, skip {v['skip']}, batch {v['batch']}   (delivered in: enforce {m['enforce']}, shadow {m['shadow']}, canary {m['canary']})")
    for t in r["topReasons"]:
        print(f"  {t['n']:>6}  {t['verdict']}:{t['reason']}")
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
        if cmd == "flush":
            print(_dump([brief(r) for r in store.take_pending()], 2))
        elif cmd == "catchup":
            print(_dump(catchup(store, parse_since(args.since), args.include_filtered), 2))
        elif cmd == "log":
            for r in store.recent_decisions(args.limit):
                tag = "" if r.mode == "enforce" else f"  [{r.mode}: woke, rules said {r.verdict}]"
                print(f"#{r.id}  {iso(r.ts)}  {r.verdict:<5}  {r.source}/{r.key}  {r.reason}{tag}")
        elif cmd == "rules":
            print(_dump(rules_status(_load(args), store), 2))
        elif cmd == "report":
            r = report(store, parse_since(args.since), cost_per_wake=args.cost_per_wake or None)
            print(_dump(r, 2)) if args.json else _print_report(r)
        elif cmd == "review":
            items = review(store, parse_since(args.since), sample=args.sample, seed=args.seed)
            if args.json:
                print(_dump(items, 2))
            else:
                print(f'{len(items)} event(s) waiting for a label. For each: wakegate label <id> wanted|unwanted\n("wanted" = it should have woken the agent)\n')
                for b in items:
                    print(f"#{b['id']}  {b['ts']}  {b['source']}/{b['key']}  {b['verdict']}:{b['reason']}\n      {_dump(b['event'].get('data'))[:200]}")
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
