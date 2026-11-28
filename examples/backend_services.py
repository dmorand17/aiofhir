"""SMART Backend Services: a server-to-server token, with no user or patient.

The client authenticates as itself (`client_credentials`) with a client secret
or a private key (`private_key_jwt`, needs `aiofhir[jwt]`), then runs one
system-level search. Use this for jobs such as nightly exports or seeding, never
for a patient-facing app.

Environment:
- BASE, CLIENT_ID.
- CLIENT_SECRET, or PRIVATE_KEY_FILE and KEY_ID (KEY_ALGORITHM: RS384 or ES384,
  default RS384), as registered with the server.
- SCOPE (optional): by default the system read scopes the server advertises.
- RESOURCE (default Patient): the type to search.

    BASE=... CLIENT_ID=my-job CLIENT_SECRET=... python examples/backend_services.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiofhir import FhirClient
from aiofhir.client import SearchResult
from aiofhir.log import configure_cli_logging
from aiofhir.smart import ClientAssertionKey, SmartClient, TokenSet


class ClientCredentialsToken:
    """A token provider for FhirClient: a backend token has no refresh token, so a
    new one is requested a minute before expiry, or once after a 401."""

    def __init__(
        self,
        smart: SmartClient,
        scope: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._smart = smart
        self._scope = scope
        self._clock = clock
        self._tokens: TokenSet | None = None
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> TokenSet | None:
        return self._tokens

    async def get(self) -> str:
        async with self._lock:
            if self._tokens is None or self._tokens.expires_at <= self._clock() + timedelta(
                seconds=60
            ):
                self._tokens = await self._smart.client_credentials(self._scope)
            return self._tokens.access_token

    async def refresh(self, stale: str | None = None) -> str:
        async with self._lock:
            if self._tokens is None or stale is None or stale == self._tokens.access_token:
                self._tokens = await self._smart.client_credentials(self._scope)
            return self._tokens.access_token


async def first_page(
    smart: SmartClient, resource: str, scope: str | None = None
) -> tuple[SearchResult, str]:
    """One page of `resource`, and the scope the server granted."""
    provider = ClientCredentialsToken(smart, scope)
    async with FhirClient(smart.base, provider, profile=smart.profile) as fhir:
        result = await fhir.search(resource, {"_count": "10"}, max_pages=1)
    assert provider.tokens is not None
    return result, provider.tokens.scope


def _smart_client() -> SmartClient:
    base, client_id = os.environ.get("BASE", ""), os.environ.get("CLIENT_ID", "")
    if not base or not client_id:
        sys.exit("Set BASE and CLIENT_ID.")
    key = None
    if key_file := os.environ.get("PRIVATE_KEY_FILE"):
        key = ClientAssertionKey(
            Path(key_file).read_bytes(),
            key_id=os.environ.get("KEY_ID", ""),
            algorithm=os.environ.get("KEY_ALGORITHM") or "RS384",
        )
    secret = os.environ.get("CLIENT_SECRET") or None
    if key is None and secret is None:
        sys.exit("Set CLIENT_SECRET, or PRIVATE_KEY_FILE and KEY_ID.")
    return SmartClient(base, client_id, redirect_uri="", client_secret=secret, private_key=key)


async def main() -> None:
    configure_cli_logging()
    smart = _smart_client()
    resource = os.environ.get("RESOURCE") or "Patient"
    result, scope = await first_page(smart, resource, os.environ.get("SCOPE") or None)
    print(f"Granted scope: {scope}")
    more = " (more on later pages)" if result.truncated else ""
    print(f"{resource}: {len(result.resources)} on the first page{more}")
    for warning in result.warnings:
        print(f"warning: {warning}")


if __name__ == "__main__":
    asyncio.run(main())
