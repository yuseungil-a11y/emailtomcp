from __future__ import annotations

import threading

from emailtomcp.core.events import EventBus


def test_publish_subscribe_basic() -> None:
    bus = EventBus()
    received: list[tuple[str, object]] = []
    bus.subscribe("new_message", lambda name, payload: received.append((name, payload)))

    bus.publish("new_message", {"id": 1})

    assert received == [("new_message", {"id": 1})]


def test_wildcard_subscriber_receives_all_events() -> None:
    bus = EventBus()
    received: list[str] = []
    bus.subscribe("*", lambda name, payload: received.append(name))

    bus.publish("a", None)
    bus.publish("b", None)

    assert received == ["a", "b"]


def test_unsubscribe_stops_future_events() -> None:
    bus = EventBus()
    received: list[str] = []

    def handler(name: str, payload: object) -> None:
        received.append(name)

    bus.subscribe("x", handler)
    bus.publish("x", None)
    bus.unsubscribe("x", handler)
    bus.publish("x", None)

    assert received == ["x"]


def test_handler_exception_does_not_break_other_handlers() -> None:
    bus = EventBus()
    calls: list[str] = []

    def bad(name: str, payload: object) -> None:
        raise RuntimeError("boom")

    def good(name: str, payload: object) -> None:
        calls.append(name)

    bus.subscribe("x", bad)
    bus.subscribe("x", good)
    bus.publish("x", None)

    assert calls == ["x"]


def test_thread_safety_many_publishers() -> None:
    bus = EventBus()
    counter = {"n": 0}
    lock = threading.Lock()

    def handler(name: str, payload: object) -> None:
        with lock:
            counter["n"] += 1

    bus.subscribe("evt", handler)

    def publish_many() -> None:
        for _ in range(100):
            bus.publish("evt", None)

    threads = [threading.Thread(target=publish_many) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert counter["n"] == 400
