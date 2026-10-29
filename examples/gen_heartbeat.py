"""Generates a synthetic week of an always-on agent: a heartbeat every 30 minutes
(almost always "nothing to do") plus a handful of real events.

    uv run python examples/gen_heartbeat.py > examples/heartbeat.jsonl
"""

import json

START = 1_767_571_200_000  # 2026-01-05 00:00 UTC
HALF_HOUR = 30 * 60_000
DAY = 86_400_000
HOUR = 3_600_000

out = []
for i in range(7 * 48):
    ts = START + i * HALF_HOUR
    # The heartbeat payload only differs by a timestamp the agent would never care about.
    out.append({"source": "heartbeat", "key": "main", "ts": ts, "data": {"status": "HEARTBEAT_OK", "checkedAt": ts}})

real = [
    (1, 9, "github", "pr/412", {"state": "review_requested", "actor": "alice"}, None),
    (1, 9.2, "github", "pr/412", {"state": "review_requested", "actor": "ci[bot]"}, None),
    (1, 14, "mail", "thread/88", {"subject": "Invoice question", "unread": 1}, None),
    (2, 3, "pagerduty", "inc/7", {"state": "triggered"}, "critical"),
    (3, 10, "github", "issue/90", {"state": "opened", "actor": "bob"}, None),
    (3, 10.1, "github", "issue/90", {"state": "labeled", "actor": "bob"}, None),
    (3, 10.2, "github", "issue/90", {"state": "commented", "actor": "bob"}, None),
    (5, 16, "mail", "thread/91", {"subject": "Contract draft", "unread": 1}, None),
]
for day, hour, source, key, data, severity in real:
    e = {"source": source, "key": key, "ts": int(START + day * DAY + hour * HOUR), "data": data}
    if severity:
        e["severity"] = severity
    out.append(e)

for e in out:
    print(json.dumps(e, separators=(",", ":")))
