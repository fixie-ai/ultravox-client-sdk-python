import asyncio
import typing

from livekit import rtc

from ultravox_client import patched_event_emitter

EVENT_TYPES = list(typing.get_args(rtc.room.EventTypes))


class RoomListener(patched_event_emitter.PatchedAsyncIOEventEmitter):
    def __init__(self, room: rtc.Room):
        super().__init__(loop=asyncio.get_running_loop())
        for event in EVENT_TYPES:
            room.on(event, self.create_propagater(event))

    def create_propagater(self, event: str):
        def propagate(*args, **kwargs):
            self.emit(event, *args, **kwargs)

        return propagate
