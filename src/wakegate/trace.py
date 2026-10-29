"""The decision trace: what each layer saw and decided, as plain JSON.

The shape is a contract (see schemas/trace.v1.schema.json), so other languages can read it.
"""

from __future__ import annotations

import json
from typing import Any

from .hashing import canonical_json, sha256
from .store import Store

TRACE_VERSION = 1


def build_trace(store: Store, decision_id: int) -> dict[str, Any]:
    d = store.get_decision(decision_id)
    if d is None:
        raise ValueError(f"no decision with id {decision_id}")
    decided_by, rows = store.get_layer_runs(decision_id)

    layers = [
        {
            "seq": r["seq"],
            "layer": r["layer"],
            "kind": r["kind"],
            "mode": r["mode"],
            "applied": bool(r["applied"]),
            "outcome": r["outcome"],
            "final": bool(r["final"]),
            "reason": r["reason"],
            **({"rule": r["rule"]} if r["rule"] else {}),
            **({"confidence": r["confidence"]} if r["confidence"] is not None else {}),
            "detail": json.loads(r["detail_json"]) if r["detail_json"] else {},
            "views": [],
            "cost": {"ms": r["duration_ms"], "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"], "usd": r["cost_usd"]},
        }
        for r in rows
    ]
    return {
        "trace_version": TRACE_VERSION,
        "decision_id": d.id,
        "event": {"source": d.source, "key": d.key, "ts": d.ts, "digest": "sha256:" + sha256(canonical_json(d.event))},
        "layers": layers,
        "result": {"verdict": d.verdict, "decided_by": decided_by, "delivered": d.action, "mode": d.mode, "reason": d.reason},
        # Decisions made before traces existed have no layer rows; only the summary above is known.
        "synthesized": not rows,
    }
