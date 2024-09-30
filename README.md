# Ultravox client SDK for Python
Python client SDK for [Ultravox](https://ultravox.ai).

[![pypi-v](https://img.shields.io/pypi/v/ultravox-client.svg?label=ultravox-client&color=orange)](https://pypi.org/project/ultravox-client/)

## Getting started

```bash
pip install ultravox-client
```

## Project structure

* ultravox-client contains the client SDK code
* example contains a working example

This project uses [Poetry](https://python-poetry.org/) to manage dependencies along with [Just](https://just.systems/) for shorthand commands.

## Publishing to PyPi
1. Bump version number in `ultravox_client/pyproject.toml`
1. (in the `ultravox_client` directory) Run `poetry publish --build -u __token__ -p <your_pypi_token>`