"""Discover a SMART server and print its endpoints, resource types and the derived scope.

Reads BASE from the environment. A .env file in the repo root or in this
directory is loaded first, and an already-set BASE is left as-is.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from aiofhir.capability import fetch_capability_statement, patient_search_types, readable_types
from aiofhir.log import configure_cli_logging
from aiofhir.profiles import HasteProfile
from aiofhir.smart.discovery import discover
from aiofhir.smart.scopes import scopes_from_capabilities

_ROOT = Path(__file__).resolve().parents[1]


def _base_url() -> str:
    load_dotenv(_ROOT / ".env")
    load_dotenv(_ROOT / "examples" / ".env")
    base = os.environ.get("BASE", "").strip()
    if not base:
        print("Set BASE to the FHIR base URL, or add it to .env.", file=sys.stderr)
        raise SystemExit(1)
    return base


async def main() -> None:
    configure_cli_logging()
    base = _base_url()
    async with httpx.AsyncClient() as http:
        configuration = await discover(base, HasteProfile(), http)
        statement = await fetch_capability_statement(base, http)
    readable = readable_types(statement)
    scope = scopes_from_capabilities(configuration, resources=readable)
    print(f"base: {base.rstrip('/')}")
    print(f"authorization_endpoint: {configuration.authorization_endpoint}")
    print(f"token_endpoint: {configuration.token_endpoint}")
    print(f"readable types: {', '.join(readable)}")
    print(f"patient-searchable types: {', '.join(sorted(patient_search_types(statement)))}")
    print(f"scope: {scope}")


if __name__ == "__main__":
    asyncio.run(main())
