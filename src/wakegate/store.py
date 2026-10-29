from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .timeutil import iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
  source TEXT NOT NULL, key TEXT NOT NULL,
  fingerprint TEXT NOT NULL, last_seen_ts INTEGER NOT NULL, last_wake_ts INTEGER,
  PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, day TEXT NOT NULL,
  source TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
  action TEXT NOT NULL, verdict TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'enforce',
  reason TEXT NOT NULL, rule TEXT,
  changed INTEGER NOT NULL, event_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_ts ON decisions (ts);
CREATE INDEX IF NOT EXISTS decisions_day_action ON decisions (day, action);
CREATE TABLE IF NOT EXISTS pending (decision_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS labels (
  decision_id INTEGER PRIMARY KEY, label TEXT NOT NULL, note TEXT, ts INTEGER NOT NULL,
  sample INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS review_queue (decision_id INTEGER PRIMARY KEY, picked_ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS http_validators (
  url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT
);
"""


@dataclass
class KeyState:
    fingerprint: str
    last_seen_ts: int
    last_wake_ts: int | None


@dataclass
class DecisionRow:
    id: int
    ts: int
    day: str
    source: str
    key: str
    fingerprint: str
    #: What was delivered.
    action: str
    #: What the rules decided.
    verdict: str
    mode: str
    reason: str
    rule: str | None
    changed: bool
    event: dict[str, Any]


def brief(r: DecisionRow) -> dict[str, Any]:
    """The compact form of a logged decision, used for batches and the audit log."""
    return {
        "id": r.id,
        "ts": iso(r.ts),
        "source": r.source,
        "key": r.key,
        "action": r.action,
        "verdict": r.verdict,
        "mode": r.mode,
        "reason": r.reason,
        "event": r.event,
    }


def _row(r: sqlite3.Row) -> DecisionRow:
    return DecisionRow(
        id=r["id"], ts=r["ts"], day=r["day"], source=r["source"], key=r["key"], fingerprint=r["fingerprint"],
        action=r["action"], verdict=r["verdict"], mode=r["mode"], reason=r["reason"], rule=r["rule"],
        changed=bool(r["changed"]), event=json.loads(r["event_json"]),
    )


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: we manage transactions ourselves.
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(_SCHEMA)
        self._migrate()
        self._depth = 0

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        """Databases made before shadow/canary lack the verdict and mode columns."""
        cols = {c["name"] for c in self._db.execute("PRAGMA table_info(decisions)")}
        if "verdict" not in cols:
            self._db.execute("ALTER TABLE decisions ADD COLUMN verdict TEXT NOT NULL DEFAULT ''")
        if "mode" not in cols:
            self._db.execute("ALTER TABLE decisions ADD COLUMN mode TEXT NOT NULL DEFAULT 'enforce'")
        self._db.execute("UPDATE decisions SET verdict = action WHERE verdict = ''")

    def transaction(self):
        """Atomic block, so concurrent CLI invocations cannot interleave a decision. Re-entrant."""
        return _Transaction(self)

    # ---- per-key state
    def get_state(self, source: str, key: str) -> KeyState | None:
        r = self._db.execute(
            "SELECT fingerprint, last_seen_ts, last_wake_ts FROM state WHERE source=? AND key=?", (source, key)
        ).fetchone()
        return KeyState(r["fingerprint"], r["last_seen_ts"], r["last_wake_ts"]) if r else None

    def put_state(self, source: str, key: str, fingerprint: str, ts: int, woke: bool) -> None:
        self._db.execute(
            """INSERT INTO state (source, key, fingerprint, last_seen_ts, last_wake_ts) VALUES (?,?,?,?,?)
               ON CONFLICT (source, key) DO UPDATE SET fingerprint=excluded.fingerprint, last_seen_ts=excluded.last_seen_ts,
                 last_wake_ts=CASE WHEN ? THEN excluded.last_seen_ts ELSE last_wake_ts END""",
            (source, key, fingerprint, ts, ts if woke else None, 1 if woke else 0),
        )

    # ---- decisions
    def insert_decision(self, *, ts: int, day: str, source: str, key: str, fingerprint: str, action: str,
                        verdict: str, mode: str, reason: str, rule: str | None, changed: bool, event: dict[str, Any]) -> int:
        cur = self._db.execute(
            """INSERT INTO decisions (ts, day, source, key, fingerprint, action, verdict, mode, reason, rule, changed, event_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, day, source, key, fingerprint, action, verdict, mode, reason, rule, 1 if changed else 0,
             json.dumps(event, ensure_ascii=False, separators=(",", ":"))),
        )
        return int(cur.lastrowid)

    def count_wakes(self, day: str) -> int:
        """Wakes the rules approved today. Counted by verdict, so shadow mode simulates the budget and canaries do not spend it."""
        return self._db.execute("SELECT COUNT(*) AS n FROM decisions WHERE day=? AND verdict='wake'", (day,)).fetchone()["n"]

    def get_decision(self, id: int) -> DecisionRow | None:
        r = self._db.execute("SELECT * FROM decisions WHERE id=?", (id,)).fetchone()
        return _row(r) if r else None

    def decisions_since(self, ts: int, limit: int = 10_000) -> list[DecisionRow]:
        return [_row(r) for r in self._db.execute("SELECT * FROM decisions WHERE ts >= ? ORDER BY ts, id LIMIT ?", (ts, limit))]

    def recent_decisions(self, limit: int) -> list[DecisionRow]:
        rows = [_row(r) for r in self._db.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))]
        return rows[::-1]

    # ---- batch
    def add_pending(self, decision_id: int) -> None:
        self._db.execute("INSERT OR IGNORE INTO pending (decision_id) VALUES (?)", (decision_id,))

    def take_pending(self) -> list[DecisionRow]:
        """Returns and clears the batch."""
        with self.transaction():
            rows = [
                _row(r)
                for r in self._db.execute(
                    "SELECT d.* FROM decisions d JOIN pending p ON p.decision_id = d.id ORDER BY d.ts, d.id"
                )
            ]
            self._db.execute("DELETE FROM pending")
        return rows

    def pending_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) AS n FROM pending").fetchone()["n"]

    # ---- labels and review sampling
    def set_label(self, id: int, label: str, note: str | None, ts: int) -> bool:
        """Records a judgement. Returns True when it counts as a random sample (a canary or a review draw)."""
        d = self.get_decision(id)
        if d is None:
            raise ValueError(f"no decision with id {id}")
        sample = d.mode == "canary" or self.in_queue(id)
        self._db.execute(
            """INSERT INTO labels (decision_id, label, note, ts, sample) VALUES (?,?,?,?,?)
               ON CONFLICT (decision_id) DO UPDATE SET label=excluded.label, note=excluded.note, ts=excluded.ts, sample=excluded.sample""",
            (id, label, note, ts, 1 if sample else 0),
        )
        return sample

    def labels(self) -> dict[int, dict[str, Any]]:
        return {
            r["decision_id"]: {"label": r["label"], "note": r["note"], "sample": r["sample"] == 1}
            for r in self._db.execute("SELECT decision_id, label, note, sample FROM labels")
        }

    def in_queue(self, id: int) -> bool:
        return self._db.execute("SELECT 1 FROM review_queue WHERE decision_id=?", (id,)).fetchone() is not None

    def add_to_queue(self, ids: list[int], ts: int) -> None:
        for i in ids:
            self._db.execute("INSERT OR IGNORE INTO review_queue (decision_id, picked_ts) VALUES (?,?)", (i, ts))

    def queued_ids(self) -> list[int]:
        return [r["decision_id"] for r in self._db.execute("SELECT decision_id FROM review_queue ORDER BY decision_id")]

    # ---- poll validators
    def get_validators(self, url: str) -> tuple[str | None, str | None] | None:
        r = self._db.execute("SELECT etag, last_modified FROM http_validators WHERE url=?", (url,)).fetchone()
        return (r["etag"], r["last_modified"]) if r else None

    def put_validators(self, url: str, etag: str | None, last_modified: str | None) -> None:
        self._db.execute(
            """INSERT INTO http_validators (url, etag, last_modified) VALUES (?,?,?)
               ON CONFLICT (url) DO UPDATE SET etag=excluded.etag, last_modified=excluded.last_modified""",
            (url, etag, last_modified),
        )


class _Transaction:
    def __init__(self, store: Store) -> None:
        self._s = store

    def __enter__(self) -> None:
        s = self._s
        if s._depth == 0:
            s._db.execute("BEGIN IMMEDIATE")
        s._depth += 1

    def __exit__(self, exc_type, exc, tb) -> None:
        s = self._s
        s._depth -= 1
        if s._depth == 0:
            s._db.execute("ROLLBACK" if exc_type else "COMMIT")
