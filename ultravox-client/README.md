# Ultravox client SDK for Python
Python client SDK for [Ultravox](https://ultravox.ai).

[![pypi-v](https://img.shields.io/pypi/v/ultravox-client.svg?label=ultravox-client&color=orange)](https://pypi.org/project/ultravox-client/)

## Getting started

```bash
pip install ultravox-client
```

## Usage

```python
import asyncio
import signal

import ultravox_client as uv

session = uv.UltravoxSession()
state = await session.joinCall(os.getenv("JOIN_URL", None))
done = asyncio.Event()
loop = asyncio.get_running_loop()

@state.on("status")
def on_status():
    if state.status == uv.UltravoxSessionStatus.DISCONNECTED:
        done.set()

loop.add_signal_handler(signal.SIGINT, lambda: done.set())
loop.add_signal_handler(signal.SIGTERM, lambda: done.set())
await done.wait()
await session.leaveCall()
```

See the included example app for a more complete example. To get a `joinUrl`, you'll want to integrate your server with the [Ultravox REST API](https://fixie-ai.github.io/ultradox/).