"""Sign in with a browser as the seeded patient and print the Haste record helpers.

Reads BASE from the environment or a .env file. The smoke client is the one
haste/seed.py creates: SMOKE_CLIENT_ID (default aiofhir-smoke) and
SMOKE_CLIENT_SECRET, which the seed saves to .env when it creates the client.
CLIENT_ID and CLIENT_SECRET are the admin seed-login client and are not used.

Sign in as the patient user (SEED_USER_EMAIL), not as the admin. Use a private
window, or set SMOKE_NO_BROWSER=1 to print the URL instead of opening it.
The redirect URI is http://localhost:8765/callback.
"""

from __future__ import annotations

import asyncio
import os
import sys
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

from aiofhir.log import configure_cli_logging
from haste.login import authorization, capture_callback, run_smoke, wait_for_code
from haste.seeding import CLIENT_ID as SMOKE_CLIENT_ID

_ROOT = Path(__file__).resolve().parents[1]


def _load() -> None:
    load_dotenv(_ROOT / ".env")
    load_dotenv(_ROOT / "haste" / ".env")


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Set {name}.", file=sys.stderr)
        raise SystemExit(1)
    return value


async def _main() -> None:
    _load()
    configure_cli_logging()
    base = _env("BASE")
    client_id = os.environ.get("SMOKE_CLIENT_ID", "").strip() or SMOKE_CLIENT_ID
    client_secret = os.environ.get("SMOKE_CLIENT_SECRET", "").strip() or None
    request = await authorization(base, client_id, client_secret)
    server, future = await capture_callback(request.state)
    async with server:
        print("Sign in as the seeded patient user. Open this URL in a private window:")
        print(request.url)
        if os.environ.get("SMOKE_NO_BROWSER", "").strip() != "1":
            webbrowser.open(request.url)
        code = await wait_for_code(future)
    print(await run_smoke(base, client_id, client_secret, code, request.code_verifier))


if __name__ == "__main__":
    asyncio.run(_main())
