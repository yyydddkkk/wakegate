"""Golden outputs: the refactor to layers must not change a single decision.

The fixtures in tests/golden were generated from the pre-layer implementation, whose benchmark
output had been compared byte for byte with the original TypeScript version. Regenerate them only
when behaviour is changed on purpose:  UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py
"""

import hashlib
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from synthetic_traffic import generate

import bench_canary
import bench_synthetic
from wakegate import Gate, Store, parse_config, read_events, replay

GOLDEN = Path(__file__).parent / "golden"
EXAMPLES = Path(__file__).parent.parent / "examples"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


def check(name: str, actual: str) -> None:
    path = GOLDEN / name
    if UPDATE or not path.exists():
        path.write_text(actual)
        if not UPDATE:
            pytest.fail(f"created missing golden file {name}; review it and rerun")
        return
    assert actual == path.read_text(), f"{name} changed"


def decision_digests() -> dict[str, str]:
    """A hash of every decision (action, verdict, mode, reason, rule, carried batch) per config and seed."""
    configs = {name: yaml for name, yaml in bench_synthetic.CONFIGS}
    configs["canary 20% on tuned"] = "canary: { rate: 0.2 }\n" + bench_synthetic.tuned("cooldownSeconds: 1800", True)
    configs["shadow on tuned"] = "mode: shadow\n" + bench_synthetic.tuned("cooldownSeconds: 1800", True)
    out = {}
    for name, yaml in configs.items():
        for seed in (1000, 1001):
            store = Store(":memory:")
            gate = Gate(parse_config(yaml), store)
            h = hashlib.sha256()
            for item in generate(seed):
                d = gate.decide(item.event)
                row = [d.id, d.action, d.verdict, d.mode, d.reason, d.rule, d.changed, [b["id"] for b in d.batch or []]]
                h.update(json.dumps(row).encode())
            store.close()
            out[f"{name} / seed {seed}"] = h.hexdigest()[:16]
    return out


def test_every_decision_is_unchanged():
    check("decisions.json", json.dumps(decision_digests(), indent=2, sort_keys=True) + "\n")


def run_main(module, argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        module.main(argv)
    return buf.getvalue()


def test_stress_test_output_is_unchanged():
    check("bench_synthetic_2.txt", run_main(bench_synthetic, ["2"]))


def test_canary_benchmark_output_is_unchanged():
    check("bench_canary_6.txt", run_main(bench_canary, ["6"]))


def test_heartbeat_replay_is_unchanged():
    from wakegate import load_config

    r = replay(read_events(EXAMPLES / "heartbeat.jsonl"), load_config(EXAMPLES / "wakegate.yaml"), 1.4)
    check("replay_heartbeat.json", json.dumps(r.__dict__, indent=2, sort_keys=True) + "\n")
