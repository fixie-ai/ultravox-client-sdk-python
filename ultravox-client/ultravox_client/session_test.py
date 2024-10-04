import asyncio
from collections import defaultdict
from unittest import mock
from typing import Any, AsyncGenerator, Callable

import json
import pytest
import websockets
from livekit import rtc

from ultravox_client import audio
from ultravox_client import session


class FakeRoom:
    def __init__(self):
        self.listeners: dict[str, list[Callable]] = defaultdict(list)
        self.local_participant = mock.AsyncMock(spec=rtc.LocalParticipant)

    def on(self, event: str, listener: Callable):
        self.listeners[event].append(listener)

    def emit(self, event: str, *args, **kwargs):
        for listener in self.listeners[event]:
            listener(*args, **kwargs)

    async def connect(self, room_url: str, token: str):
        pass

    async def disconnect(self):
        pass


@pytest.fixture(autouse=True)
def fake_room(mocker):
    room = FakeRoom()
    mocker.patch("livekit.rtc.Room", return_value=room)
    mocker.patch("ultravox_client.session._AudioSourceToSendTrackAdapter.start")
    mocker.patch("ultravox_client.session._AudioSinkFromRecvTrackAdapter.start")
    return room


class FakeWsServer:
    def __init__(self):
        super().__init__()
        self._messages = asyncio.Queue()
        self.open = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self._messages.get()
        if message is None:
            raise StopAsyncIteration
        if isinstance(message, Exception):
            if isinstance(message, websockets.ConnectionClosed):
                self.open = False
            raise message
        return message

    @property
    def response_headers(self):
        return {}

    def add_message(self, message: str):
        self._add(message)

    def add_error(self, error: Exception):
        self._add(error)

    def flush(self):
        self._add(None)

    def _add(self, message):
        self._messages.put_nowait(message)

    def reset(self, url: str):
        self._messages = asyncio.Queue()
        self.open = True

    @property
    def closed(self):
        return not self.open

    async def close(self):
        self.open = False
        self.flush()

    async def send(self, message):
        raise AssertionError(f"Unexpected web socket message sent by client: {message}")


@pytest.fixture(autouse=True)
async def fake_ws_server(mocker):
    server = FakeWsServer()

    async def side_effect(url, extra_headers=None):
        server.reset(url)
        server.add_message(
            '{"type":"room_info", "roomUrl": "wss://some-url", "token": "banana"}'
        )
        return server

    mocker.patch("websockets.connect", side_effect=side_effect)
    yield server
    server.flush()


class FakeAudioSource(audio.AudioSource):
    async def stream(self) -> AsyncGenerator[bytes, None]:
        yield b"\0" * 3200


class FakeAudioSink(audio.AudioSink):
    def write(self, data: bytes) -> None:
        pass

    async def close(self) -> None:
        pass


async def test_client_tool_implementation(fake_room):
    s = session.UltravoxSession()

    async def tool_impl(params: dict[str, Any]):
        assert params == {"foo": "bar"}
        await asyncio.sleep(0)
        return "baz"

    s.register_tool_implementation("test_tool", tool_impl)
    await s.join_call("wss://test.ultravox.ai", FakeAudioSource(), FakeAudioSink())
    await asyncio.sleep(0.001)

    data_packet = rtc.DataPacket(
        data=json.dumps(
            {
                "type": "client_tool_invocation",
                "toolName": "test_tool",
                "invocationId": "call_1",
                "parameters": {"foo": "bar"},
            }
        ).encode(),
        kind=rtc.DataPacketKind.KIND_RELIABLE,
    )
    fake_room.emit("data_received", data_packet)
    await asyncio.sleep(0.001)
    fake_room.local_participant.publish_data.assert_called_once_with(
        json.dumps(
            {"type": "client_tool_result", "invocationId": "call_1", "result": "baz"}
        ).encode("utf-8")
    )
    await s.leave_call()


async def test_client_tool_implementation_with_response_type(fake_room):
    s = session.UltravoxSession()

    def tool_impl(params: dict[str, Any]):
        assert params == {"foo": "bar"}
        return '{"strict": true}', "hang-up"

    s.register_tool_implementation("test_tool", tool_impl)
    await s.join_call("wss://test.ultravox.ai", FakeAudioSource(), FakeAudioSink())
    await asyncio.sleep(0.001)

    data_packet = rtc.DataPacket(
        data=json.dumps(
            {
                "type": "client_tool_invocation",
                "toolName": "test_tool",
                "invocationId": "call_1",
                "parameters": {"foo": "bar"},
            }
        ).encode(),
        kind=rtc.DataPacketKind.KIND_RELIABLE,
    )
    fake_room.emit("data_received", data_packet)
    await asyncio.sleep(0.001)
    fake_room.local_participant.publish_data.assert_called_once_with(
        json.dumps(
            {
                "type": "client_tool_result",
                "invocationId": "call_1",
                "result": '{"strict": true}',
                "responseType": "hang-up",
            }
        ).encode("utf-8")
    )
    await s.leave_call()
