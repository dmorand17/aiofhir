# List available commands.
default:
    @just --list

# Install the project and dev tools from uv.lock.
sync:
    uv sync --locked --extra dev

# Refresh uv.lock.
lock:
    uv lock

# Run pytest. Extra arguments are passed through.
test *args:
    uv run pytest {{args}}

# Lint with ruff.
lint:
    uv run ruff check .

# Format with ruff.
fmt:
    uv run ruff format .

# Check formatting without writing files.
fmt-check:
    uv run ruff format --check .

# Typecheck with mypy strict.
typecheck:
    uv run mypy

# Apply ruff autofixes and format.
fix:
    uv run ruff check --fix .
    uv run ruff format .

# Run the same checks as CI: sync, lint, format, mypy, pytest.
check: sync lint fmt-check typecheck test
