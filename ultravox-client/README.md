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
done = asyncio.Event()

@session.on("status")
def on_status():
    if session.status == uv.UltravoxSessionStatus.DISCONNECTED:
        done.set()

await session.join_call(os.getenv("JOIN_URL", None))
loop = asyncio.get_running_loop()
loop.add_signal_handler(signal.SIGINT, lambda: done.set())
loop.add_signal_handler(signal.SIGTERM, lambda: done.set())
await done.wait()
await session.leave_call()
```

See the included example app for a more complete example. To get a `joinUrl`, you'll want to integrate your server with the [Ultravox REST API](https://fixie-ai.github.io/ultradox/).
