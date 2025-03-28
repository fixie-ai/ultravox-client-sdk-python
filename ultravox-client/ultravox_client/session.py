import asyncio
import contextlib
import dataclasses
import enum
import inspect
import json
import logging
import urllib.parse
from importlib import metadata
from typing import Any, Awaitable, Callable, Literal, Tuple

import dataclasses_json
from livekit import rtc
from websockets.asyncio import client as ws_client
from websockets import exceptions as ws_exceptions

from ultravox_client import async_close
from ultravox_client import audio
from ultravox_client import patched_event_emitter
from ultravox_client import room_listener


class _AudioSourceToSendTrackAdapter:
    """Adapter than takes in an AudioSource and writes from it to a LiveKit audio track."""

    def __init__(self, source: audio.AudioSource):
        self._source = source
        self._rtc_source = rtc.AudioSource(source.sample_rate, source.num_channels)
        self._task: asyncio.Task | None = None
        self._enabled = True

    @property
    def track(self):
        if not self._track:
            raise Exception("track not initialized")
        return self._track

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = value

    def start(self):
        self._track = rtc.LocalAudioTrack.create_audio_track("input", self._rtc_source)
        self._task = asyncio.create_task(self._pump())

    async def close(self):
        await async_close.async_cancel(self._task)

    async def _pump(self):
        async for chunk in self._source.stream():
            if not self._enabled:
                chunk = b"\x00" * len(chunk)
            frame = rtc.AudioFrame(
                chunk,
                self._source.sample_rate,
                self._source.num_channels,
                len(chunk) // (self._source.num_channels * 2),
            )
            await self._rtc_source.capture_frame(frame)


class _AudioSinkFromRecvTrackAdapter:
    """Adapter that takes in a LiveKit audio track and reads from it to an AudioSink (e.g., a speaker)."""

    def __init__(self, sink: audio.AudioSink):
        super().__init__()
        self._sink = sink
        self._task: asyncio.Task | None = None
        self._enabled = True

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = value

    def start(self, track: rtc.Track):
        self._task = asyncio.create_task(self._pump(rtc.AudioStream(track)))

    async def close(self):
        await async_close.async_close(
            async_close.async_cancel(self._task), self._sink.close()
        )

    async def _pump(self, stream: rtc.AudioStream):
        async with contextlib.AsyncExitStack() as stack:
            stack.push_async_callback(stream.aclose)
            async for chunk in stream:
                data = chunk.frame.data.tobytes()
                self._sink.write(data if self._enabled else b"\x00" * len(data))


class UltravoxSessionStatus(enum.Enum):
    """The current status of an UltravoxSession."""

    DISCONNECTED = enum.auto()
    """The voice session is not connected and not attempting to connect. This is the initial state of a voice session."""
    DISCONNECTING = enum.auto()
    """The client is disconnecting from the voice session."""
    CONNECTING = enum.auto()
    """The client is attempting to connect to the voice session."""
    IDLE = enum.auto()
    """The client is connected to the voice session and the server is warming up."""
    LISTENING = enum.auto()
    """The client is connected and the server is listening for voice input."""
    THINKING = enum.auto()
    """The client is connected and the server is considering its response. The user can still interrupt."""
    SPEAKING = enum.auto()
    """The client is connected and the server is playing response audio. The user can interrupt as needed."""

    def is_live(self):
        return self in {
            UltravoxSessionStatus.IDLE,
            UltravoxSessionStatus.LISTENING,
            UltravoxSessionStatus.THINKING,
            UltravoxSessionStatus.SPEAKING,
        }


Role = Literal["user", "agent"]
Medium = Literal["text", "voice"]
AgentReaction = Literal["speaks", "listens", "speaks-once"]


@dataclasses.dataclass(frozen=True, kw_only=True)
class ClientToolResult(dataclasses_json.DataClassJsonMixin):
    """The result of a client tool invocation."""

    dataclass_json_config = dataclasses_json.config(
        letter_case=dataclasses_json.LetterCase.CAMEL
    )["dataclasses_json"]

    result: str
    """The result of the tool invocation."""
    response_type: str = "tool-response"
    """The response type for the tool invocation. See https://docs.ultravox.ai/essentials/tools#changing-call-state"""
    agent_reaction: AgentReaction = "speaks"
    """How the agent should react to the tool invocation. See https://docs.ultravox.ai/essentials/tools#controlling-agent-responses-to-tools"""
    update_call_state: dict[str, Any] | None = None
    """The new call state, if it should change. Call state can be sent to other tools using an automatic parameter."""


ClientToolImplementation = Callable[
    [dict[str, Any]],
    str
    | Awaitable[str]
    | Tuple[str, str]
    | Awaitable[Tuple[str, str]]
    | ClientToolResult
    | Awaitable[ClientToolResult],
]


@dataclasses.dataclass(frozen=True)
class Transcript:
    """A transcription of a single utterance."""

    text: str
    """The possibly-incomplete text of an utterance."""
    final: bool
    """Whether the text is complete or the utterance is ongoing."""
    speaker: Role
    """Who emitted the utterance."""
    medium: Medium
    """The medium through which the utterance was emitted."""


class UltravoxSession(patched_event_emitter.PatchedAsyncIOEventEmitter):
    """Manages a single session with Ultravox and emits events to notify
    consumers of state changes.  The following events are emitted:

      - "status": emitted when the status of the session changes.
      - "transcripts": emitted when a transcript is added or updated.
      - "experimental_message": emitted when an experimental message is received.
           The message is included as the first argument to the event handler.
      - "mic_muted": emitted when the user's microphone is muted or unmuted.
      - "speaker_muted": emitted when the user's speaker (i.e. output audio from the agent) is muted or unmuted.
      - "data_message": emitted when any data message is received (including those
           typically handled by this SDK). See https://docs.ultravox.ai/api/data_messages.
           The message is included as the first argument to the event handler.
    """

    def __init__(self, experimental_messages: set[str] | None = None) -> None:
        super().__init__()
        self._transcripts: list[Transcript | None] = []
        self._status = UltravoxSessionStatus.DISCONNECTED

        self._room: rtc.Room | None = None
        self._room_listener: room_listener.RoomListener | None = None
        self._socket: ws_client.ClientConnection | None = None
        self._receive_task: asyncio.Task | None = None
        self._source_adapter: _AudioSourceToSendTrackAdapter | None = None
        self._sink_adapter: _AudioSinkFromRecvTrackAdapter | None = None
        self._experimental_messages = experimental_messages or set()
        self._registered_tools: dict[str, ClientToolImplementation] = {}

    @property
    def status(self):
        return self._status

    @property
    def transcripts(self):
        return [t for t in self._transcripts if t is not None]

    @property
    def mic_muted(self) -> bool:
        """Indicates whether the microphone is muted."""
        return not self._source_adapter.enabled if self._source_adapter else False

    @mic_muted.setter
    def mic_muted(self, mute: bool) -> None:
        """Sets the mute state of the user's microphone."""
        if self.mic_muted != mute:
            if self._source_adapter:
                self._source_adapter.enabled = not mute
            self.emit("mic_muted", mute)

    def toggle_mic_muted(self) -> None:
        """Toggles the mute state of the user's microphone."""
        self.mic_muted = not self.mic_muted

    @property
    def speaker_muted(self) -> bool:
        """Indicates whether the user's speaker (i.e. output audio from the agent) is muted."""
        return not self._sink_adapter.enabled if self._sink_adapter else False

    @speaker_muted.setter
    def speaker_muted(self, mute: bool) -> None:
        """Sets the mute state of the user's speaker (i.e. output audio from the agent)."""
        if self.speaker_muted != mute:
            if self._sink_adapter:
                self._sink_adapter.enabled = not mute
            self.emit("speaker_muted", mute)

    def toggle_speaker_muted(self) -> None:
        """Toggles the mute state of the user's speaker (i.e. output audio from the agent)."""
        self.speaker_muted = not self.speaker_muted

    async def set_output_medium(self, medium: Medium) -> None:
        """Sets the agent's output medium. If the agent is currently speaking, this will
        take effect at the end of the agent's utterance. Also see speaker_muted above."""
        await self.send_data({"type": "set_output_medium", "medium": medium})

    def register_tool_implementation(
        self, name: str, tool_impl: ClientToolImplementation
    ) -> None:
        """Registers a client tool implementation with the given name. If the
        call is started with a client-implemented tool, this implementation will
        be invoked when the model calls the tool.

        The implementation should accept a single argument, a dict[str, Any] of
        parameters defined by the tool, and return a string or a tuple of two
        strings where the first is the result value and the second is the
        response type (for affecting the call itself, e.g. to have the agent
        hang up). The implementation may optionally be async.

        See https://docs.ultravox.ai/tools for more information."""
        self._registered_tools[name] = tool_impl

    def register_tool_implementations(
        self, impls: dict[str, ClientToolImplementation]
    ) -> None:
        """Convenience batch wrapper on register_tool_implementation."""
        self._registered_tools.update(impls)

    async def join_call(
        self,
        join_url: str,
        source: audio.AudioSource | None = None,
        sink: audio.AudioSink | None = None,
        client_version: str | None = None,
    ) -> None:
        """Connects to a call using the given joinUrl."""
        if self._status != UltravoxSessionStatus.DISCONNECTED:
            raise RuntimeError("Cannot join a new call while already in a call.")
        self._update_status(UltravoxSessionStatus.CONNECTING)

        url_parts = list(urllib.parse.urlparse(join_url))
        query = dict(urllib.parse.parse_qsl(url_parts[4]))
        if self._experimental_messages:
            query["experimentalMessages"] = ",".join(self._experimental_messages)
        uv_client_version = f"python_{metadata.version('ultravox-client')}"
        if client_version:
            uv_client_version += f":{client_version}"
        query["clientVersion"] = uv_client_version
        query["apiVersion"] = "1"
        url_parts[4] = urllib.parse.urlencode(query)
        join_url = urllib.parse.urlunparse(url_parts)

        self._socket = await ws_client.connect(join_url)
        self._source_adapter = _AudioSourceToSendTrackAdapter(
            source or audio.LocalAudioSource()
        )
        self._sink_adapter = _AudioSinkFromRecvTrackAdapter(
            sink or audio.LocalAudioSink()
        )
        self._receive_task = asyncio.create_task(self._socket_receive())

    async def leave_call(self):
        """Leaves the current call (if any)."""
        await self._disconnect()

    async def send_text(self, text: str, deferResponse: bool | None = None):
        """Sends a message via text."""
        if not self._status.is_live():
            raise RuntimeError(
                f"Cannot send text while not connected. Current status is {self.status}"
            )
        data: dict[str, Any] = {"type": "input_text_message", "text": text}
        if deferResponse is not None:
            data["deferResponse"] = deferResponse
        await self.send_data(data)

    async def send_data(self, msg: dict):
        """Sends a data message to the Ultravox server. See https://docs.ultravox.ai/api/data_messages."""
        if not self._room or not self._socket:
            raise RuntimeError("Cannot send data while not connected")
        if "type" not in msg:
            raise ValueError("Message must have a 'type' field")
        msg_str = json.dumps(msg)
        msg_bytes = msg_str.encode("utf-8")
        if len(msg_bytes) > 1024:
            # Larger messages don't reliably make it to the server via UDP,
            # so we use our websocket instead.
            await self._socket.send(msg_str)
        else:
            await self._room.local_participant.publish_data(msg_bytes)

    async def _socket_receive(self):
        assert self._socket
        try:
            async for message in self._socket:
                if isinstance(message, str):
                    await self._on_message(message)
        except Exception as e:
            if not isinstance(e, ws_exceptions.ConnectionClosed):
                logging.exception("UltravoxSession websocket error", exc_info=e)
        await self._disconnect()

    async def _on_message(self, payload: str):
        msg = json.loads(payload)
        match msg.get("type", None):
            case "room_info":
                self._room = rtc.Room()
                self._room_listener = room_listener.RoomListener(self._room)
                self._room_listener.add_listener(
                    "track_subscribed", self._on_track_subscribed
                )
                self._room_listener.add_listener(
                    "data_received", self._on_data_received
                )

                await self._room.connect(msg["roomUrl"], msg["token"])
                self._update_status(UltravoxSessionStatus.IDLE)
                opts = rtc.TrackPublishOptions()
                opts.source = rtc.TrackSource.SOURCE_MICROPHONE
                assert self._source_adapter
                self._source_adapter.start()
                await self._room.local_participant.publish_track(
                    self._source_adapter.track, opts
                )

    async def _disconnect(self):
        if self._status == UltravoxSessionStatus.DISCONNECTED:
            return
        self._update_status(UltravoxSessionStatus.DISCONNECTING)
        await async_close.async_close(
            self._source_adapter.close() if self._source_adapter else None,
            self._sink_adapter.close() if self._sink_adapter else None,
            self._room.disconnect() if self._room else None,
            async_close.async_cancel(self._receive_task),
            self._socket.close() if self._socket else None,
        )
        self._room = None
        self._socket = None
        self._source_adapter = None
        self._sink_adapter = None
        self._receive_task = None
        self._update_status(UltravoxSessionStatus.DISCONNECTED)

    def _on_track_subscribed(
        self,
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.Participant,
    ):
        assert self._sink_adapter
        self._sink_adapter.start(track)

    async def _on_data_received(self, data_packet: rtc.DataPacket):
        msg = json.loads(data_packet.data.decode("utf-8"))
        assert isinstance(msg, dict)
        self.emit("data_message", msg)
        match msg.get("type", None):
            case "state":
                match msg.get("state", None):
                    case "listening":
                        self._update_status(UltravoxSessionStatus.LISTENING)
                    case "thinking":
                        self._update_status(UltravoxSessionStatus.THINKING)
                    case "speaking":
                        self._update_status(UltravoxSessionStatus.SPEAKING)
            case "transcript":
                ordinal = msg.get("ordinal", -1)
                medium = msg.get("medium", "voice")
                role = msg.get("role", "agent")
                final = msg.get("final", False)
                self._add_or_update_transcript(
                    ordinal,
                    medium,
                    role,
                    final,
                    text=msg.get("text", None),
                    delta=msg.get("delta", None),
                )
            case "client_tool_invocation":
                await self._invoke_client_tool(
                    msg["toolName"], msg["invocationId"], msg["parameters"]
                )
            case _:
                if self._experimental_messages:
                    self.emit("experimental_message", msg)

    async def _invoke_client_tool(
        self, tool_name: str, invocation_id: str, parameters: dict[str, Any]
    ):
        if tool_name not in self._registered_tools:
            logging.warning(
                f"Client tool {tool_name} was invoked but is not registered"
            )
            result_msg = {
                "type": "client_tool_result",
                "invocationId": invocation_id,
                "errorType": "undefined",
                "errorMessage": f"Client tool {tool_name} is not registered (Python client)",
            }
            await self.send_data(result_msg)
            return
        try:
            result = self._registered_tools[tool_name](parameters)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, str):
                result = ClientToolResult(result=result)
            elif isinstance(result, tuple):
                result = ClientToolResult(result=result[0], response_type=result[1])
            result_msg = {
                "type": "client_tool_result",
                "invocationId": invocation_id,
                **result.to_dict(),
            }
            await self.send_data(result_msg)
        except Exception as e:
            logging.exception(f"Error invoking client tool {tool_name}", exc_info=e)
            result_msg = {
                "type": "client_tool_result",
                "invocationId": invocation_id,
                "errorType": "implementation-error",
                "errorMessage": str(e),
            }
            await self.send_data(result_msg)

    def _update_status(self, status: UltravoxSessionStatus):
        if self._status == status:
            return
        self._status = status
        self.emit("status")

    def _add_or_update_transcript(
        self,
        ordinal: int,
        medium: Medium,
        role: Role,
        final: bool,
        *,
        text: str | None = None,
        delta: str | None = None,
    ):
        present_text = text or delta or ""
        while len(self._transcripts) < ordinal:
            self._transcripts.append(None)
        if len(self._transcripts) == ordinal:
            self._transcripts.append(Transcript(present_text, final, role, medium))
        else:
            if text is not None:
                new_text = text
            else:
                prior_transcript = self._transcripts[ordinal]
                prior_text = prior_transcript.text if prior_transcript else ""
                new_text = prior_text + (delta or "")
            self._transcripts[ordinal] = Transcript(new_text, final, role, medium)
        self.emit("transcripts")
