import { sha256 } from "./hash.ts";
import type { Store } from "./store.ts";
import type { WakeEvent } from "./types.ts";

export type PollResult =
  | { kind: "not-modified" }
  | { kind: "event"; event: WakeEvent };

export interface PollOptions {
  source?: string;
  timeoutMs?: number;
  userAgent?: string;
  fetch?: typeof fetch;
}

/**
 * One conditional GET. Sends If-None-Match / If-Modified-Since from the last
 * response, so unchanged resources cost a 304. The body is hashed and never
 * stored or put in the event.
 *
 * A single poll is deliberately dumb: scheduling is the caller's job (cron,
 * a timer). Honour `pollIntervalHint` (GitHub's x-poll-interval, Retry-After)
 * when you choose the next run.
 */
export async function pollUrl(url: string, store: Store, opts: PollOptions = {}): Promise<PollResult & { pollIntervalHint?: number }> {
  const doFetch = opts.fetch ?? fetch;
  const prev = store.getValidators(url);
  const headers: Record<string, string> = { "user-agent": opts.userAgent ?? "wakegate/0.1" };
  if (prev?.etag) headers["if-none-match"] = prev.etag;
  if (prev?.lastModified) headers["if-modified-since"] = prev.lastModified;

  const res = await doFetch(url, { headers, signal: AbortSignal.timeout(opts.timeoutMs ?? 15_000) });
  const hint = Number(res.headers.get("x-poll-interval") ?? res.headers.get("retry-after") ?? NaN);
  const pollIntervalHint = Number.isFinite(hint) ? hint : undefined;

  if (res.status === 304) return { kind: "not-modified", pollIntervalHint };

  const body = Buffer.from(await res.arrayBuffer());
  const etag = res.headers.get("etag");
  const lastModified = res.headers.get("last-modified");
  store.putValidators(url, { etag, lastModified });

  return {
    kind: "event",
    pollIntervalHint,
    event: {
      source: opts.source ?? new URL(url).host,
      key: url,
      data: { status: res.status, bodyHash: sha256(body.toString("latin1")), bytes: body.length },
    },
  };
}
