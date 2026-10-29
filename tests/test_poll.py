from wakegate import Store, poll_url
from wakegate.poll import HttpResponse


def test_poll_sends_validators_from_the_previous_response_and_treats_304_as_no_event():
    store = Store(":memory:")
    seen = []

    def fake(url, headers, timeout):
        seen.append(headers)
        if len(seen) == 1:
            return HttpResponse(200, {"etag": '"v1"', "x-poll-interval": "60"}, b"hello")
        return HttpResponse(304, {})

    first = poll_url("https://example.test/feed", store, fetch=fake)
    assert first.kind == "event"
    assert first.poll_interval_hint == 60
    assert first.event.source == "example.test"
    assert "hello" not in str(first.event.to_dict()), "body must not leak into the event"

    second = poll_url("https://example.test/feed", store, fetch=fake)
    assert second.kind == "not-modified"
    assert seen[1]["if-none-match"] == '"v1"'
