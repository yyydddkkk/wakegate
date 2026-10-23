import { getPath } from "./hash.ts";
import type { WakeEvent } from "./types.ts";

/**
 * Declarative conditions. No code execution: a condition is data, so a config
 * file can be reviewed and audited without trusting it.
 *
 *   { all: [c, ...] } | { any: [c, ...] } | { not: c }
 *   { path: "data.actor", eq | ne | in | matches | contains | exists | gt | gte | lt | lte: ... }
 */
export type Predicate = (event: WakeEvent) => boolean;

const LEAF_OPS = ["eq", "ne", "in", "matches", "contains", "exists", "gt", "gte", "lt", "lte"] as const;
const MAX_REGEX_INPUT = 10_000;

export function compileCondition(raw: unknown, where: string): Predicate {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error(`${where}: condition must be an object`);
  }
  const obj = raw as Record<string, unknown>;

  if ("all" in obj || "any" in obj) {
    const kind = "all" in obj ? "all" : "any";
    const list = obj[kind];
    if (!Array.isArray(list) || list.length === 0) throw new Error(`${where}.${kind}: expected a non-empty list`);
    const subs = list.map((c, i) => compileCondition(c, `${where}.${kind}[${i}]`));
    return kind === "all" ? (e) => subs.every((p) => p(e)) : (e) => subs.some((p) => p(e));
  }
  if ("not" in obj) {
    const sub = compileCondition(obj.not, `${where}.not`);
    return (e) => !sub(e);
  }

  if (typeof obj.path !== "string") throw new Error(`${where}: expected "path" or all/any/not`);
  const path = obj.path;
  const ops = LEAF_OPS.filter((op) => op in obj);
  const unknown = Object.keys(obj).filter((k) => k !== "path" && !(LEAF_OPS as readonly string[]).includes(k));
  if (unknown.length) throw new Error(`${where}: unknown key "${unknown[0]}"`);
  if (ops.length !== 1) throw new Error(`${where}: exactly one operator required (${LEAF_OPS.join(", ")})`);
  const op = ops[0]!;
  const arg = obj[op];
  const get = (e: WakeEvent) => getPath(e, path);

  switch (op) {
    case "eq": return (e) => get(e) === arg;
    case "ne": return (e) => get(e) !== arg;
    case "in":
      if (!Array.isArray(arg)) throw new Error(`${where}.in: expected a list`);
      return (e) => arg.includes(get(e));
    case "matches": {
      if (typeof arg !== "string") throw new Error(`${where}.matches: expected a string`);
      const re = new RegExp(arg);
      return (e) => {
        const v = get(e);
        return typeof v === "string" && v.length <= MAX_REGEX_INPUT && re.test(v);
      };
    }
    case "contains":
      if (typeof arg !== "string") throw new Error(`${where}.contains: expected a string`);
      return (e) => {
        const v = get(e);
        return typeof v === "string" ? v.includes(arg) : Array.isArray(v) && v.includes(arg);
      };
    case "exists":
      return (e) => (get(e) !== undefined && get(e) !== null) === Boolean(arg);
    default: {
      if (typeof arg !== "number") throw new Error(`${where}.${op}: expected a number`);
      const cmp = { gt: (a: number) => a > arg, gte: (a: number) => a >= arg, lt: (a: number) => a < arg, lte: (a: number) => a <= arg }[op]!;
      return (e) => {
        const v = get(e);
        return typeof v === "number" && cmp(v);
      };
    }
  }
}
