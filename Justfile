# To install Just, see: https://github.com/casey/just#installation

# Allow for positional arguments in Just receipes.
set positional-arguments := true

# Dump the Python stack trace on crashes.
export PYTHONFAULTHANDLER := "1"

# Default recipe that runs if you type "just".
default: format check test

# Install dependencies for local development.
install:
    curl -LsSf https://astral.sh/uv/install.sh | sh
    cd ultravox-client && uv sync
    cd example && uv sync

# Format code.
format: format-sdk format-example

format-sdk: (format-python "ultravox-client")

format-example: (format-python "example")

format-python PATH="ultravox-client":
    cd {{PATH}} && uv run ruff format
    cd {{PATH}} && uv run ruff check --fix-only

# Analyze code.
check: check-sdk check-example

check-sdk: (check-python "ultravox-client")

check-example: (check-python "example")

check-python PATH="ultravox-client":
    cd {{PATH}} && uv run ruff format --check
    cd {{PATH}} && uv run ruff check
    cd {{PATH}} && uv run pyright
    cd {{PATH}} && uv run deptry .

# Run tests.
test: test-sdk

test-sdk *ARGS="--dist loadgroup -n auto .":
    cd ultravox-client && uv run pytest --ignore third_party {{ARGS}}

# Format, analyze, and test one package.
sdk: format-sdk check-sdk test-sdk

example: format-example check-example # example has no tests right now

# Run the Ultravox example.
run-example *FLAGS:
    cd example && uv run python ultravox_example/client.py {{FLAGS}}
