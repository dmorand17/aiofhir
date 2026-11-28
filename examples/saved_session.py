"""Resume a saved SMART session without signing in again.

Loads the tokens that standalone_launch.py saved to TOKENS_FILE, reads the
signed-in patient, and writes every refreshed token set back to the file. When
the refresh token is no longer accepted, aiofhir raises `AuthExpired` and the
user has to sign in again.

Environment: BASE, CLIENT_ID, CLIENT_SECRET (for a confidential client) and
TOKENS_FILE, the same values standalone_launch.py was run with.

    TOKENS_FILE=tokens.json BASE=... CLIENT_ID=my-app python examples/standalone_launch.py
    TOKENS_FILE=tokens.json BASE=... CLIENT_ID=my-app python examples/saved_session.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from aiofhir import AuthExpired, FhirClient, PatientRecord
from aiofhir.log import configure_cli_logging
from aiofhir.smart import RefreshingToken, SmartClient, TokenSet


def load_tokens(path: Path) -> TokenSet:
    return TokenSet.from_dict(json.loads(path.read_text()))


def save_tokens(path: Path, tokens: TokenSet) -> None:
    """Write the tokens readable by this user only. Encrypt them at rest in a real app."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(tokens.to_dict(), handle)


async def read_patient(smart: SmartClient, path: Path) -> dict[str, Any] | None:
    """The signed-in patient's Patient resource, refreshing the token when it is due."""
    tokens = load_tokens(path)
    if tokens.patient is None:
        raise RuntimeError("the saved token has no patient")

    async def persist(refreshed: TokenSet) -> None:
        # Refresh tokens may rotate: the old one stops working, so save every new set.
        save_tokens(path, refreshed)

    holder = RefreshingToken(smart, tokens, on_refresh=persist)
    async with FhirClient(smart.base, holder, profile=smart.profile) as fhir:
        found = await PatientRecord(fhir, patient_id=tokens.patient).search_raw("Patient")
    return found.resources[0] if found.resources else None


def display_name(patient: dict[str, Any]) -> str:
    for name in patient.get("name") or []:
        if name.get("text"):
            return str(name["text"])
        full = " ".join([*name.get("given", []), name.get("family", "")]).strip()
        if full:
            return full
    return "(no name)"


async def main() -> None:
    configure_cli_logging()
    base, client_id = os.environ.get("BASE", ""), os.environ.get("CLIENT_ID", "")
    tokens_file = os.environ.get("TOKENS_FILE", "")
    if not base or not client_id or not tokens_file:
        sys.exit("Set BASE, CLIENT_ID and TOKENS_FILE.")
    path = Path(tokens_file)
    if not path.exists():
        sys.exit(f"{path} does not exist. Sign in first with standalone_launch.py.")
    smart = SmartClient(
        base,
        client_id=client_id,
        redirect_uri="",  # not used: this script only refreshes
        client_secret=os.environ.get("CLIENT_SECRET") or None,
    )
    try:
        patient = await read_patient(smart, path)
    except AuthExpired as exc:
        sys.exit(f"{exc} Sign in again with standalone_launch.py.")
    print(display_name(patient) if patient else "The server returned no Patient for this token.")
    print(f"Token valid until {load_tokens(path).expires_at:%Y-%m-%d %H:%M} UTC.")


if __name__ == "__main__":
    asyncio.run(main())
