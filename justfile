# List available commands.
default:
    @just --list

# Install the project and dev tools from uv.lock.
sync:
    uv sync --locked --all-extras

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

# Discover SMART endpoints and the derived scope for BASE.
discover:
    uv run python examples/discover.py

# Seed Haste. SYNTHEA_DIR defaults to haste/synthea-data. LOG_LEVEL=DEBUG traces requests.
seed:
    SYNTHEA_DIR="{{ env_var_or_default('SYNTHEA_DIR', 'haste/synthea-data') }}" uv run python -m haste.seed

# Delete the admin seed client (aiofhir-seed). Post haste/bundles/create-seed-client.json to seed again.
seed-cleanup:
    uv run python -m haste.seed --cleanup-client

# Check the patient access policy with $evaluate-policy and that aiofhir-seed needs its secret.
check-policy:
    uv run python -m haste.check_policy

# check-policy, then sign in as the patient user with user/*.rs and send the read cases for real.
check-policy-as-patient:
    uv run python -m haste.check_policy --as-patient

# Sign in with the browser and print the record helpers.
smoke:
    uv run python -m haste.smoke

# Run the aiofhir[server] reference backend. See src/aiofhir/server/__main__.py for its env.
serve:
    uv run python -m aiofhir.server

# Run the Streamlit demo on port 8765, the redirect URI the seeded smoke client allows.
demo:
    uv run --group demo streamlit run examples/streamlit_app.py --server.address localhost --server.port 8765 --server.headless true --browser.gatherUsageStats false
