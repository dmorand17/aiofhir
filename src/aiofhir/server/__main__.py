"""Run the reference backend: `python -m aiofhir.server` (or `just serve`).

Required environment:
- AIOFHIR_FHIR_BASE: the FHIR base URL.
- AIOFHIR_CLIENT_ID: the SMART client registered on that server.
- AIOFHIR_REDIRECT_URI: this backend's `/auth/callback`, registered on the client.
- AIOFHIR_SERVICE_TOKEN: the bearer the app backend and agents send.
- AIOFHIR_FERNET_KEY: a Fernet key (`Fernet.generate_key()`).

Optional: AIOFHIR_CLIENT_SECRET, AIOFHIR_PROFILE (generic, haste, epic or
cerner; default generic), AIOFHIR_HOST (default 127.0.0.1), AIOFHIR_PORT
(default 8000). Tokens are kept in memory, so they are lost on restart.

Access logs are off because the callback URL carries the authorization code
and state. httpx stays at WARNING so FHIR search URLs (patient ids) are not
logged.
"""

from __future__ import annotations

import os
import sys

import uvicorn

from aiofhir.log import configure_cli_logging
from aiofhir.profiles import CernerProfile, EpicProfile, GenericProfile, HasteProfile
from aiofhir.profiles.base import ServerProfile
from aiofhir.server.app import create_app
from aiofhir.server.store import EncryptedStore

_PROFILES: dict[str, type[ServerProfile]] = {
    "generic": GenericProfile,
    "haste": HasteProfile,
    "epic": EpicProfile,
    "cerner": CernerProfile,
}


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Set {name}.", file=sys.stderr)
        raise SystemExit(1)
    return value


def main() -> None:
    configure_cli_logging()
    profile_name = os.environ.get("AIOFHIR_PROFILE", "generic").strip() or "generic"
    if profile_name not in _PROFILES:
        print(f"AIOFHIR_PROFILE must be one of {sorted(_PROFILES)}.", file=sys.stderr)
        raise SystemExit(1)
    app = create_app(
        fhir_base_url=_required("AIOFHIR_FHIR_BASE"),
        client_id=_required("AIOFHIR_CLIENT_ID"),
        redirect_uri=_required("AIOFHIR_REDIRECT_URI"),
        service_token=_required("AIOFHIR_SERVICE_TOKEN"),
        store=EncryptedStore(_required("AIOFHIR_FERNET_KEY")),
        client_secret=os.environ.get("AIOFHIR_CLIENT_SECRET", "").strip() or None,
        profile=_PROFILES[profile_name](),
    )
    uvicorn.run(
        app,
        host=os.environ.get("AIOFHIR_HOST", "127.0.0.1"),
        port=int(os.environ.get("AIOFHIR_PORT", "8000")),
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
