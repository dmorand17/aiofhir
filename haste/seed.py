"""Create the aiofhir smoke client and seed appointments on a Haste project.

Reads BASE from the environment, the repo-root .env, or haste/.env. When HASTE_TOKEN
is missing, signs in with the existing CLIENT_ID and keeps the token in memory.
Set SAVE_HASTE_TOKEN=1 to also write it to .env (mode 600).
PATIENT_ID is written after the seed patient is created or reused.

The patient login: set SEED_USER_EMAIL to an address you can read (for example
you+patient@example.com). The seed finds or invites that tenant User in the
system project, links its Membership to the seed Patient, and assigns a
read-only AccessPolicyV2. An invited user must accept the email before signing
in. If the admin token cannot write the system project, set SYSTEM_TOKEN, or
invite the user in the admin console and set SEED_USER_ID. SYSTEM_BASE
overrides the derived /w/{tenant}/system/api/v1/fhir.

When the seed creates the aiofhir-smoke client, its secret is saved to .env as
SMOKE_CLIENT_SECRET (mode 600) and not printed.
SYNTHEA_DIR loads that directory's information files on every run.
The bundle entries use ifNoneExist, so an existing index is left in place.

The admin token and the client secret are never printed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from aiofhir.log import configure_cli_logging
from aiofhir.profiles import HasteProfile
from aiofhir.scrub import scrub
from aiofhir.smart.discovery import discover
from haste.login import SEED_LOGIN_SCOPE, login_access_token, save_env_value
from haste.seeding import CLIENT_ID, SeedUserError, apply_seed

_ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger("haste.seed")


def _env(name: str) -> str:
    load_dotenv(_ROOT / ".env")
    load_dotenv(_ROOT / "haste" / ".env")
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Set {name}.", file=sys.stderr)
        raise SystemExit(1)
    return value


def _optional(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def _optional_bundle(directory: str, prefix: str) -> dict[str, Any] | None:
    if not directory:
        return None
    matches = sorted(Path(directory).glob(f"{prefix}*.json"))
    if not matches:
        print(f"No {prefix} file in {directory}.", file=sys.stderr)
        raise SystemExit(1)
    loaded = json.loads(matches[0].read_text())
    if not isinstance(loaded, dict):
        raise SystemExit(f"{matches[0]} was not a JSON object")
    return loaded


async def _token() -> str:
    load_dotenv(_ROOT / ".env")
    load_dotenv(_ROOT / "haste" / ".env")
    token = os.environ.get("HASTE_TOKEN", "").strip()
    if token:
        return token
    client_id = os.environ.get("CLIENT_ID", "").strip()
    if not client_id:
        print(
            "Set HASTE_TOKEN, or CLIENT_ID for a client that already exists.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    secret = os.environ.get("CLIENT_SECRET", "").strip() or None
    scope = os.environ.get("SCOPE", "").strip() or SEED_LOGIN_SCOPE
    token = await login_access_token(_env("BASE"), client_id, secret, scope=scope)
    if os.environ.get("SAVE_HASTE_TOKEN", "").strip() == "1":
        save_env_value(_ROOT / ".env", "HASTE_TOKEN", token)
        print("Saved HASTE_TOKEN to .env (mode 600). Remove it when you are done seeding.")
    return token


def _save_client_secret(secret: str) -> None:
    """Saved as soon as the client exists, so a later failure cannot lose it."""
    save_env_value(_ROOT / ".env", "SMOKE_CLIENT_SECRET", secret)


async def _main() -> None:
    configure_cli_logging()
    base = _env("BASE")
    token = await _token()
    synthea = os.environ.get("SYNTHEA_DIR", "").strip()
    logger.info("seeding %s", base.rstrip("/"))
    async with httpx.AsyncClient(timeout=30.0) as http:
        configuration = await discover(base, HasteProfile(), http)
        try:
            result = await apply_seed(
                http,
                base,
                token,
                now=datetime.now(UTC),
                patient_id=None,
                configuration=configuration,
                secret=secrets.token_urlsafe(32),
                practitioner_bundle=_optional_bundle(synthea, "practitionerInformation"),
                hospital_bundle=_optional_bundle(synthea, "hospitalInformation"),
                seed_user_email=_optional("SEED_USER_EMAIL"),
                seed_user_id=_optional("SEED_USER_ID"),
                system_base=_optional("SYSTEM_BASE"),
                system_token=_optional("SYSTEM_TOKEN"),
                on_client_secret=_save_client_secret,
            )
        except SeedUserError as exc:
            print(f"Patient user: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
    if result.patient_id:
        save_env_value(_ROOT / ".env", "PATIENT_ID", result.patient_id)
    lines = [f"patient_id: {result.patient_id}", f"client_id: {CLIENT_ID}"]
    if result.created_secret:
        lines.append("client_secret: saved to .env as SMOKE_CLIENT_SECRET")
    else:
        lines.append("client_secret: unchanged (client already existed)")
    lines.append(f"appointments_created: {result.appointments_created}")
    lines.append(f"chart: {'loaded' if result.chart_loaded else 'skipped'}")
    lines.append(f"patient_user: {result.user_status}")
    if result.user_status == "invited":
        lines.append("  accept the invitation email before running the smoke script")
    lines.append("membership: linked" if result.membership_linked else "membership: skipped")
    lines.append(f"read_policy: {'assigned' if result.policy_assigned else 'skipped'}")
    print(scrub("\n".join(lines)))


if __name__ == "__main__":
    asyncio.run(_main())
