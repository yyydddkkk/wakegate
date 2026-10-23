import { readFileSync } from "node:fs";
import { parse } from "yaml";
import { compileCondition, type Predicate } from "./condition.ts";
import type { Action } from "./types.ts";

export interface Rule {
  name: string;
  when: Predicate;
  /** The condition as written in the config, kept so it can be shown back. */
  condition: unknown;
  then: Action;
}

export interface Config {
  /** Paths inside `data` that are ignored when fingerprinting (timestamps, counters...). */
  ignorePaths: string[];
  /** Skip events whose fingerprint equals the last one seen for the same key. Default true. */
  dedupe: boolean;
  /** Events matching a bypass rule always wake. They skip cooldown, quiet hours and budget. */
  bypass: Rule[];
  /** First matching rule decides. */
  rules: Rule[];
  /** Used when no rule matches. Default "wake". */
  defaultAction: Action;
  /** After waking for a key, further changes within this window are batched. */
  cooldownSeconds: number;
  budget: {
    maxWakesPerDay?: number;
    /** What to do with a wake that exceeds the budget. Default "batch". */
    overBudget: "batch" | "skip";
    quietHours?: { start: string; end: string };
    /** IANA zone for quiet hours and the day boundary. Default: system zone. */
    timezone?: string;
  };
}

const ACTIONS = ["wake", "skip", "batch"] as const;
const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;

function asAction(v: unknown, where: string): Action {
  if (typeof v === "string" && (ACTIONS as readonly string[]).includes(v)) return v as Action;
  throw new Error(`${where}: expected one of ${ACTIONS.join(", ")}`);
}

function compileRules(raw: unknown, where: string, forced?: Action): Rule[] {
  if (raw === undefined) return [];
  if (!Array.isArray(raw)) throw new Error(`${where}: expected a list`);
  return raw.map((r, i) => {
    const at = `${where}[${i}]`;
    if (r === null || typeof r !== "object") throw new Error(`${at}: expected an object`);
    const o = r as Record<string, unknown>;
    if (typeof o.name !== "string" || !o.name) throw new Error(`${at}: "name" is required (it appears in the audit log)`);
    return {
      name: o.name,
      when: compileCondition(o.when, `${at}.when`),
      condition: o.when,
      then: forced ?? asAction(o.then, `${at}.then`),
    };
  });
}

export function parseConfig(text: string): Config {
  const raw = (parse(text) ?? {}) as Record<string, unknown>;
  if (raw.version !== undefined && raw.version !== 1) throw new Error(`unsupported config version ${String(raw.version)}`);

  const b = (raw.budget ?? {}) as Record<string, unknown>;
  const budget: Config["budget"] = { overBudget: "batch" };
  if (b.maxWakesPerDay !== undefined) {
    if (!Number.isInteger(b.maxWakesPerDay) || (b.maxWakesPerDay as number) < 0) throw new Error("budget.maxWakesPerDay: expected a non-negative integer");
    budget.maxWakesPerDay = b.maxWakesPerDay as number;
  }
  if (b.overBudget !== undefined) {
    if (b.overBudget !== "batch" && b.overBudget !== "skip") throw new Error('budget.overBudget: expected "batch" or "skip"');
    budget.overBudget = b.overBudget;
  }
  if (b.quietHours !== undefined) {
    const q = b.quietHours as Record<string, unknown>;
    if (typeof q.start !== "string" || typeof q.end !== "string" || !HHMM.test(q.start) || !HHMM.test(q.end)) {
      throw new Error('budget.quietHours: expected {start: "HH:MM", end: "HH:MM"}');
    }
    budget.quietHours = { start: q.start, end: q.end };
  }
  if (b.timezone !== undefined) {
    if (typeof b.timezone !== "string") throw new Error("budget.timezone: expected a string");
    try {
      new Intl.DateTimeFormat("en", { timeZone: b.timezone });
    } catch {
      throw new Error(`budget.timezone: unknown zone "${b.timezone}"`);
    }
    budget.timezone = b.timezone;
  }

  const cooldown = raw.cooldownSeconds ?? 0;
  if (typeof cooldown !== "number" || cooldown < 0) throw new Error("cooldownSeconds: expected a non-negative number");

  const ignore = raw.ignorePaths ?? [];
  if (!Array.isArray(ignore) || ignore.some((p) => typeof p !== "string")) throw new Error("ignorePaths: expected a list of strings");

  return {
    ignorePaths: ignore as string[],
    dedupe: raw.dedupe === undefined ? true : Boolean(raw.dedupe),
    bypass: compileRules(raw.bypass, "bypass", "wake"),
    rules: compileRules(raw.rules, "rules"),
    defaultAction: raw.default === undefined ? "wake" : asAction(raw.default, "default"),
    cooldownSeconds: cooldown,
    budget,
  };
}

export function loadConfig(path: string): Config {
  return parseConfig(readFileSync(path, "utf8"));
}

/** The active policy as plain data, for showing to a person or an agent. */
export function describeConfig(c: Config) {
  const rule = (r: Rule) => ({ name: r.name, when: r.condition, then: r.then });
  return {
    dedupe: c.dedupe,
    ignorePaths: c.ignorePaths,
    bypass: c.bypass.map(rule),
    rules: c.rules.map(rule),
    default: c.defaultAction,
    cooldownSeconds: c.cooldownSeconds,
    budget: c.budget,
  };
}
