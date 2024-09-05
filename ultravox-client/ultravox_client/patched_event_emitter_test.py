import asyncio

import pytest

from ultravox_client import patched_event_emitter


def test_synchronous_fail():
    failures = 0

    def fail(e):
        nonlocal failures
        failures += 1
        raise RuntimeError("Fail!")

    emitter = patched_event_emitter.PatchedAsyncIOEventEmitter()
    emitter.on("error", fail)
    emitter.emit("error", Exception("test"))
    assert failures == 1


async def test_async_fail():
    failures = 0

    async def async_fail(e):
        nonlocal failures
        failures += 1
        await asyncio.sleep(0)
        raise RuntimeError("Fail!")

    emitter = patched_event_emitter.PatchedAsyncIOEventEmitter()
    emitter.on("error", async_fail)
    emitter.emit("error", Exception("test"))

    await asyncio.sleep(0.2)
    assert failures == 1


def test_decorator_fail():
    failures = 0
    emitter = patched_event_emitter.PatchedAsyncIOEventEmitter()

    @emitter.on("error")
    def fail(e):
        nonlocal failures
        failures += 1
        raise RuntimeError("Fail!")

    emitter.emit("error", Exception("test"))
    assert failures == 1


async def test_async_decorator_fail():
    failures = 0
    emitter = patched_event_emitter.PatchedAsyncIOEventEmitter()

    @emitter.on("error")
    async def async_fail(e):
        nonlocal failures
        failures += 1
        await asyncio.sleep(0)
        raise RuntimeError("Fail!")

    emitter.emit("error", Exception("test"))
    await asyncio.sleep(0.2)
    assert failures == 1


def test_remove_listener_works():
    did_run = False
    emitter = patched_event_emitter.PatchedAsyncIOEventEmitter()

    def listener():
        nonlocal did_run
        did_run = True

    emitter.on("error", listener)
    emitter.remove_listener("error", listener)
    with pytest.raises(Exception):
        emitter.emit("error", Exception("test"))
    assert did_run is False
