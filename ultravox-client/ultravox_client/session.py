import asyncio
import contextlib
import dataclasses
import enum
import inspect
import json
import logging
import urllib.parse
from typing import Any, Awaitable, Callable, Literal, Tuple

import websockets
from livekit import rtc

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
                self._sink.write(
                    chunk.data.tobytes()
                    if self._enabled
                    else b"\x00" * len(chunk.data.tobytes())
                )


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


ClientToolImplementation = Callable[
    [dict[str, Any]],
    str | Awaitable[str] | Tuple[str, str] | Awaitable[Tuple[str, str]],
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
    medium: Literal["text", "voice"]
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
    """

    def __init__(self, experimental_messages: set[str] | None = None) -> None:
        super().__init__()
        self._transcripts: list[Transcript] = []
        self._status = UltravoxSessionStatus.DISCONNECTED

        self._room: rtc.Room | None = None
        self._room_listener: room_listener.RoomListener | None = None
        self._socket: websockets.WebSocketClientProtocol | None = None
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
        return self._transcripts.copy()

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
    ) -> None:
        """Connects to a call using the given joinUrl."""
        if self._status != UltravoxSessionStatus.DISCONNECTED:
            raise RuntimeError("Cannot join a new call while already in a call.")
        self._update_status(UltravoxSessionStatus.CONNECTING)
        if self._experimental_messages:
            url_parts = list(urllib.parse.urlparse(join_url))
            query = dict(urllib.parse.parse_qsl(url_parts[4]))
            query["experimentalMessages"] = ",".join(self._experimental_messages)
            url_parts[4] = urllib.parse.urlencode(query)
            join_url = urllib.parse.urlunparse(url_parts)
        self._socket = await websockets.connect(join_url)
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

    async def send_text(self, text: str):
        """Sends a message via text. The agent will also respond via text."""
        if not self._status.is_live():
            raise RuntimeError(
                f"Cannot send text while not connected. Current status is {self.status}"
            )
        await self._send_data({"type": "input_text_message", "text": text})

    async def _socket_receive(self):
        assert self._socket
        try:
            async for message in self._socket:
                if isinstance(message, str):
                    await self._on_message(message)
        except Exception as e:
            if not isinstance(e, websockets.ConnectionClosed):
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
                transcript = Transcript(
                    msg["transcript"]["text"],
                    msg["transcript"]["final"],
                    "user",
                    msg["transcript"]["medium"],
                )
                self._add_or_update_transcript(transcript)
            case "voice_synced_transcript" | "agent_text_transcript":
                medium = "voice" if msg["type"] == "voice_synced_transcript" else "text"
                if msg.get("text", None):
                    transcript = Transcript(
                        msg["text"], msg.get("final", False), "agent", medium
                    )
                    self._add_or_update_transcript(transcript)
                elif msg.get("delta", None):
                    last_transcript = (
                        self._transcripts[-1] if self._transcripts else None
                    )
                    if last_transcript and last_transcript.speaker == "agent":
                        transcript = Transcript(
                            last_transcript.text + msg["delta"],
                            msg.get("final", False),
                            "agent",
                            medium,
                        )
                        self._add_or_update_transcript(transcript)
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
            await self._send_data(result_msg)
            return
        try:
            result = self._registered_tools[tool_name](parameters)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, tuple):
                val = result[0]
                response_type = result[1]
            else:
                val = result
                response_type = None
            assert isinstance(val, str)
            result_msg = {
                "type": "client_tool_result",
                "invocationId": invocation_id,
                "result": val,
            }
            if response_type:
                assert isinstance(response_type, str)
                result_msg["responseType"] = response_type
            await self._send_data(result_msg)
        except Exception as e:
            logging.exception(f"Error invoking client tool {tool_name}", exc_info=e)
            result_msg = {
                "type": "client_tool_result",
                "invocationId": invocation_id,
                "errorType": "implementation-error",
                "errorMessage": str(e),
            }
            await self._send_data(result_msg)

    async def _send_data(self, msg: dict):
        assert self._room
        await self._room.local_participant.publish_data(json.dumps(msg).encode("utf-8"))

    def _update_status(self, status: UltravoxSessionStatus):
        if self._status == status:
            return
        self._status = status
        self.emit("status")

    def _add_or_update_transcript(self, transcript: Transcript):
        if (
            self._transcripts
            and not self._transcripts[-1].final
            and self._transcripts[-1].speaker == transcript.speaker
        ):
            self._transcripts[-1] = transcript
        else:
            self._transcripts.append(transcript)
        self.emit("transcripts")
