import abc
import asyncio
from collections import deque
from typing import AsyncGenerator

import numpy as np


class AudioSource(abc.ABC):
    """Base class for audio sources."""

    def __init__(self, sample_rate: int = 48000, num_channels: int = 1):
        self._sample_rate = sample_rate
        self._num_channels = num_channels

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def num_channels(self) -> int:
        return self._num_channels

    @abc.abstractmethod
    def stream(self) -> AsyncGenerator[bytes, None]:
        """Streams PCM data in the specified format."""


class AudioSink(abc.ABC):
    """Abstract base class for audio sinks."""

    def __init__(self, sample_rate: int = 48000, num_channels: int = 1):
        self._sample_rate = sample_rate
        self._num_channels = num_channels

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def num_channels(self) -> int:
        return self._num_channels

    @abc.abstractmethod
    def write(self, data: bytes) -> None:
        """Writes a chunk of PCM data with the specified format."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Cleans up resources."""


class LocalAudioSource(AudioSource):
    """AudioSource that reads from the default microphone."""

    def __init__(self, sample_rate: int = 48000, channels: int = 1):
        super().__init__(sample_rate, channels)

    async def stream(self) -> AsyncGenerator[bytes, None]:
        try:
            import sounddevice
        except ImportError:
            raise ImportError(
                "The 'sounddevice' module is required for LocalAudioSource."
            )
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def callback(indata: np.ndarray, frame_count, time, status):
            loop.call_soon_threadsafe(queue.put_nowait, indata.tobytes())

        stream = sounddevice.InputStream(
            samplerate=self._sample_rate,
            channels=self._num_channels,
            callback=callback,
            device=None,
            dtype="int16",
            blocksize=self._sample_rate // 100,
        )
        with stream:
            if not stream.active:
                raise RuntimeError("Failed to open audio input stream")
            while True:
                yield await queue.get()


class LocalAudioSink(AudioSink):
    """AudioSink that plays to the default audio device."""

    def __init__(self, sample_rate: int = 48000, num_channels: int = 1) -> None:
        try:
            import sounddevice
        except ImportError:
            raise ImportError(
                "The 'sounddevice' module is required for LocalAudioSink."
            )
        super().__init__(sample_rate=sample_rate, num_channels=num_channels)
        self._queue: deque[bytes] = deque()
        self._stream: sounddevice.OutputStream | None = None

        def pull_from_queue(num_bytes: int) -> bytes:
            data = b""
            while len(data) < num_bytes and self._queue:
                chunk = self._queue.popleft()
                if len(data) + len(chunk) > num_bytes:
                    data += chunk[: num_bytes - len(data)]
                    self._queue.appendleft(chunk[num_bytes - len(data) :])
                else:
                    data += chunk
            if len(data) < num_bytes:
                data += b"\x00" * (num_bytes - len(data))
            return data

        def callback(outdata: np.ndarray, frame_count, time, status):
            data = pull_from_queue(frame_count * num_channels * 2)
            outdata[:] = np.frombuffer(data, dtype=np.int16).reshape(
                (frame_count, num_channels)
            )

        self._stream = sounddevice.OutputStream(
            samplerate=sample_rate,
            channels=num_channels,
            callback=callback,
            device=None,
            dtype="int16",
            blocksize=sample_rate // 100,
        )
        self._stream.start()
        if not self._stream.active:
            raise RuntimeError("Failed to open audio output stream")

    def write(self, data: bytes) -> None:
        self._queue.append(data)

    async def close(self) -> None:
        if self._stream:
            self._stream.close()
