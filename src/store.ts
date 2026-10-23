import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";
import type { Action, WakeEvent } from "./types.ts";

export interface KeyState {
  fingerprint: string;
  lastSeenTs: number;
  lastWakeTs: number | null;
}

export interface DecisionRow {
  id: number;
  ts: number;
  day: string;
  source: string;
  key: string;
  fingerprint: string;
  action: Action;
  reason: string;
  rule: string | null;
  changed: number;
  event: WakeEvent;
}

export interface HttpValidators {
  etag: string | null;
  lastModified: string | null;
}

const SCHEMA = `
CREATE TABLE IF NOT EXISTS state (
  source TEXT NOT NULL, key TEXT NOT NULL,
  fingerprint TEXT NOT NULL, last_seen_ts INTEGER NOT NULL, last_wake_ts INTEGER,
  PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, day TEXT NOT NULL,
  source TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
  action TEXT NOT NULL, reason TEXT NOT NULL, rule TEXT,
  changed INTEGER NOT NULL, event_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_ts ON decisions (ts);
CREATE INDEX IF NOT EXISTS decisions_day_action ON decisions (day, action);
CREATE TABLE IF NOT EXISTS pending (decision_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS http_validators (
  url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT
);
`;

export class Store {
  private db: DatabaseSync;
  private depth = 0;

  constructor(path: string) {
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true });
    this.db = new DatabaseSync(path);
    this.db.exec("PRAGMA journal_mode = WAL");
    this.db.exec(SCHEMA);
  }

  close(): void {
    this.db.close();
  }

  /** Runs `fn` atomically, so concurrent CLI invocations cannot interleave a decision. */
  transaction<T>(fn: () => T): T {
    if (this.depth > 0) return fn(); // already inside one: join it
    this.db.exec("BEGIN IMMEDIATE");
    this.depth++;
    try {
      const out = fn();
      this.db.exec("COMMIT");
      return out;
    } catch (err) {
      this.db.exec("ROLLBACK");
      throw err;
    } finally {
      this.depth--;
    }
  }

  getState(source: string, key: string): KeyState | undefined {
    const r = this.db.prepare("SELECT fingerprint, last_seen_ts, last_wake_ts FROM state WHERE source=? AND key=?").get(source, key) as
      | { fingerprint: string; last_seen_ts: number; last_wake_ts: number | null }
      | undefined;
    return r && { fingerprint: r.fingerprint, lastSeenTs: r.last_seen_ts, lastWakeTs: r.last_wake_ts };
  }

  putState(source: string, key: string, fingerprint: string, ts: number, woke: boolean): void {
    this.db
      .prepare(
        `INSERT INTO state (source, key, fingerprint, last_seen_ts, last_wake_ts) VALUES (?,?,?,?,?)
         ON CONFLICT (source, key) DO UPDATE SET fingerprint=excluded.fingerprint, last_seen_ts=excluded.last_seen_ts,
           last_wake_ts=CASE WHEN ? THEN excluded.last_seen_ts ELSE last_wake_ts END`,
      )
      .run(source, key, fingerprint, ts, woke ? ts : null, woke ? 1 : 0);
  }

  insertDecision(d: Omit<DecisionRow, "id">): number {
    const r = this.db
      .prepare(
        `INSERT INTO decisions (ts, day, source, key, fingerprint, action, reason, rule, changed, event_json)
         VALUES (?,?,?,?,?,?,?,?,?,?)`,
      )
      .run(d.ts, d.day, d.source, d.key, d.fingerprint, d.action, d.reason, d.rule, d.changed, JSON.stringify(d.event));
    return Number(r.lastInsertRowid);
  }

  countWakes(day: string): number {
    const r = this.db.prepare("SELECT COUNT(*) AS n FROM decisions WHERE day=? AND action='wake'").get(day) as { n: number };
    return r.n;
  }

  addPending(decisionId: number): void {
    this.db.prepare("INSERT OR IGNORE INTO pending (decision_id) VALUES (?)").run(decisionId);
  }

  /** Returns and clears the batch. */
  takePending(): DecisionRow[] {
    return this.transaction(() => {
      const rows = this.db
        .prepare("SELECT d.* FROM decisions d JOIN pending p ON p.decision_id = d.id ORDER BY d.ts, d.id")
        .all() as unknown as RawRow[];
      this.db.exec("DELETE FROM pending");
      return rows.map(toRow);
    });
  }

  pendingCount(): number {
    return (this.db.prepare("SELECT COUNT(*) AS n FROM pending").get() as { n: number }).n;
  }

  decisionsSince(ts: number, limit = 10_000): DecisionRow[] {
    const rows = this.db.prepare("SELECT * FROM decisions WHERE ts >= ? ORDER BY ts, id LIMIT ?").all(ts, limit) as unknown as RawRow[];
    return rows.map(toRow);
  }

  recentDecisions(limit: number): DecisionRow[] {
    const rows = this.db.prepare("SELECT * FROM decisions ORDER BY id DESC LIMIT ?").all(limit) as unknown as RawRow[];
    return rows.map(toRow).reverse();
  }

  getValidators(url: string): HttpValidators | undefined {
    const r = this.db.prepare("SELECT etag, last_modified FROM http_validators WHERE url=?").get(url) as
      | { etag: string | null; last_modified: string | null }
      | undefined;
    return r && { etag: r.etag, lastModified: r.last_modified };
  }

  putValidators(url: string, v: HttpValidators): void {
    this.db
      .prepare(
        `INSERT INTO http_validators (url, etag, last_modified) VALUES (?,?,?)
         ON CONFLICT (url) DO UPDATE SET etag=excluded.etag, last_modified=excluded.last_modified`,
      )
      .run(url, v.etag, v.lastModified);
  }
}

type RawRow = Omit<DecisionRow, "event"> & { event_json: string };

function toRow(r: RawRow): DecisionRow {
  const { event_json, ...rest } = r;
  return { ...rest, event: JSON.parse(event_json) as WakeEvent };
}

/** The compact form of a logged decision, used for batches and the audit log. */
export interface Brief {
  id: number;
  ts: string;
  source: string;
  key: string;
  action: Action;
  reason: string;
  event: WakeEvent;
}

export function brief(r: DecisionRow): Brief {
  return { id: r.id, ts: new Date(r.ts).toISOString(), source: r.source, key: r.key, action: r.action, reason: r.reason, event: r.event };
}
