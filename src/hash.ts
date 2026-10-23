import { createHash } from "node:crypto";

/** JSON with sorted object keys, so equal data always serialises equally. */
export function canonicalJson(value: unknown): string {
  return JSON.stringify(sortKeys(value)) ?? "null";
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(value).sort()) out[k] = sortKeys((value as Record<string, unknown>)[k]);
    return out;
  }
  return value;
}

export function sha256(text: string): string {
  return createHash("sha256").update(text).digest("hex");
}

export function getPath(obj: unknown, path: string): unknown {
  let cur: unknown = obj;
  for (const part of path.split(".")) {
    if (cur === null || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, unknown>)[part];
  }
  return cur;
}

/** Returns a deep copy of `data` with the given dotted paths removed. */
export function omitPaths(data: unknown, paths: readonly string[]): unknown {
  if (!paths.length || data === null || typeof data !== "object") return data;
  const copy = structuredClone(data) as Record<string, unknown>;
  for (const path of paths) {
    const parts = path.split(".");
    let cur: unknown = copy;
    for (let i = 0; i < parts.length - 1; i++) {
      if (cur === null || typeof cur !== "object") break;
      cur = (cur as Record<string, unknown>)[parts[i]!];
    }
    if (cur !== null && typeof cur === "object") delete (cur as Record<string, unknown>)[parts[parts.length - 1]!];
  }
  return copy;
}

/** Small seeded PRNG (mulberry32) so samples are reproducible. */
export function seededRandom(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
