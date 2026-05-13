import asyncio

import pytest

from ark import broker


def test_publish_no_subscriber_is_noop():
    broker.publish("nobody", {"x": 1})  # must not raise


@pytest.mark.asyncio
async def test_subscribe_publish_unsubscribe():
    q = broker.subscribe("sess")
    broker.publish("sess", {"type": "injected_message", "text": "hi"})
    evt = await asyncio.wait_for(q.get(), timeout=0.5)
    assert evt == {"type": "injected_message", "text": "hi"}
    broker.unsubscribe("sess", q)
    # After unsubscribing, no longer receives.
    broker.publish("sess", {"type": "again"})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.1)


@pytest.mark.asyncio
async def test_multiple_subscribers_each_get_event():
    q1 = broker.subscribe("dual")
    q2 = broker.subscribe("dual")
    broker.publish("dual", {"ok": True})
    e1 = await asyncio.wait_for(q1.get(), timeout=0.5)
    e2 = await asyncio.wait_for(q2.get(), timeout=0.5)
    assert e1 == {"ok": True}
    assert e2 == {"ok": True}
    broker.unsubscribe("dual", q1)
    broker.unsubscribe("dual", q2)
