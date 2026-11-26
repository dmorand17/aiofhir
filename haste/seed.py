"""Create the aiofhir smoke client and seed appointments on a Haste project.

Reads BASE from the environment or the repo-root .env, the only .env it loads
(a leftover haste/.env is ignored, with a warning). When HASTE_TOKEN is missing,
signs in with the existing CLIENT_ID and keeps the token in memory.
Set SAVE_HASTE_TOKEN=1 to also write it to .env (mode 600). If Haste answers 401,
the saved HASTE_TOKEN has most likely expired: remove it from .env and run again.
PATIENT_ID is written after the seed patient is created or reused.

The patient login: set SEED_USER_EMAIL to an address you can read (for example
you+patient@example.com). The seed finds or invites that tenant User in the
system project, links its Membership to the seed Patient, and assigns a
read-only AccessPolicyV2: reads by id only. SEED_ALLOW_SEARCH=1 also allows
type searches, which the policy cannot pin to the patient (see
haste.seeding.build_read_policy). An invited user must accept the email before
signing in. If the admin token cannot write the system project, set SYSTEM_TOKEN, or
invite the user in the admin console and set SEED_USER_ID. SYSTEM_BASE
overrides the derived /w/{tenant}/system/api/v1/fhir.

When the seed creates the aiofhir-smoke client, its secret is saved to .env as
SMOKE_CLIENT_SECRET (mode 600) and not printed.

The admin seed client (aiofhir-seed, user/*.cruds) is made confidential at the
end of every run: when it has no secret yet, a new one is saved to .env as
CLIENT_SECRET (mode 600) and set on the client, and its refresh grant is
dropped. Later runs sign in with that secret. `python -m haste.seed
--cleanup-client` (`just seed-cleanup`) deletes the client instead; post
bundles/create-seed-client.json again to re-seed.
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

from aiofhir.profiles import HasteProfile
from aiofhir.scrub import scrub
from aiofhir.smart.discovery import discover
from haste.env import ROOT, load_env, start_script
from haste.login import SEED_LOGIN_SCOPE, login_access_token, save_env_value
from haste.seeding import (
    CLIENT_ID,
    SEED_CLIENT_ID,
    AdminTokenError,
    SeedUserError,
    apply_seed,
    delete_seed_client,
    secure_seed_client,
)

_ROOT = ROOT
logger = logging.getLogger("haste.seed")


def _env(name: str) -> str:
    load_env()
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Set {name}.", file=sys.stderr)
        raise SystemExit(1)
    return value


def _allow_search() -> bool:
    """SEED_ALLOW_SEARCH=1 opens type searches; see build_read_policy for the risk."""
    return os.environ.get("SEED_ALLOW_SEARCH", "").strip() == "1"


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


async def admin_token() -> str:
    """The admin token: HASTE_TOKEN, or a browser sign-in with CLIENT_ID."""
    load_env()
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


def _save_seed_client_secret(secret: str) -> None:
    """Saved before the client is changed, so the next sign-in has it."""
    save_env_value(_ROOT / ".env", "CLIENT_SECRET", secret)


def _seed_client_id() -> str | None:
    """The admin client this run may lock: aiofhir-seed, unless CLIENT_ID names another."""
    client_id = os.environ.get("CLIENT_ID", "").strip()
    if client_id and client_id != SEED_CLIENT_ID:
        return None
    return SEED_CLIENT_ID


async def _lock_seed_client(http: httpx.AsyncClient, base: str, token: str) -> str:
    client_id = _seed_client_id()
    if client_id is None:
        return (
            f"left as is (CLIENT_ID={os.environ.get('CLIENT_ID', '').strip()}); "
            "make sure it is confidential or limited to admins"
        )
    status = await secure_seed_client(
        http,
        base,
        token,
        new_secret=secrets.token_urlsafe(32),
        on_secret=_save_seed_client_secret,
        client_id=client_id,
    )
    if status == "locked":
        return f"{client_id} locked; its new secret is in .env as CLIENT_SECRET"
    if status == "confidential":
        return f"{client_id} already confidential"
    return f"{client_id} not found"


async def _cleanup_client() -> None:
    start_script()
    base = _env("BASE")
    token = await admin_token()
    async with httpx.AsyncClient(timeout=30.0) as http:
        deleted = await delete_seed_client(http, base, token)
    if deleted:
        print(
            f"Deleted ClientApplication/{SEED_CLIENT_ID}. Remove CLIENT_SECRET from .env. "
            "To seed again, post haste/bundles/create-seed-client.json first."
        )
    else:
        print(f"ClientApplication/{SEED_CLIENT_ID} was already gone.")


async def _main() -> None:
    start_script()
    base = _env("BASE")
    token = await admin_token()
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
                allow_search=_allow_search(),
            )
        except SeedUserError as exc:
            print(f"Patient user: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        seed_client = await _lock_seed_client(http, base, token)
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
    if result.policy_assigned:
        lines.append(
            "  searches: allowed by type (SEED_ALLOW_SEARCH=1; only a patient/ scope confines them)"
            if _allow_search()
            else "  searches: denied (reads by id only; SEED_ALLOW_SEARCH=1 allows them by type)"
        )
    lines.append(f"seed_client: {seed_client}")
    print(scrub("\n".join(lines)))


def main(argv: list[str]) -> None:
    if argv not in ([], ["--cleanup-client"]):
        raise SystemExit("usage: python -m haste.seed [--cleanup-client]")
    try:
        asyncio.run(_cleanup_client() if argv else _main())
    except AdminTokenError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main(sys.argv[1:])
