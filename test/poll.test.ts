import assert from "node:assert/strict";
import { test } from "node:test";
import { pollUrl } from "../src/poll.ts";
import { Store } from "../src/store.ts";

test("poll sends validators from the previous response and treats 304 as no event", async () => {
  const store = new Store(":memory:");
  const seen: Array<Record<string, string>> = [];
  const fake = (async (_url: string, init: { headers: Record<string, string> }) => {
    seen.push(init.headers);
    return seen.length === 1
      ? new Response("hello", { status: 200, headers: { etag: '"v1"', "x-poll-interval": "60" } })
      : new Response(null, { status: 304 });
  }) as unknown as typeof fetch;

  const first = await pollUrl("https://example.test/feed", store, { fetch: fake });
  assert.equal(first.kind, "event");
  assert.equal(first.pollIntervalHint, 60);
  if (first.kind === "event") {
    assert.equal(first.event.source, "example.test");
    assert.equal(JSON.stringify(first.event).includes("hello"), false, "body must not leak into the event");
  }

  const second = await pollUrl("https://example.test/feed", store, { fetch: fake });
  assert.equal(second.kind, "not-modified");
  assert.equal(seen[1]?.["if-none-match"], '"v1"');
});
