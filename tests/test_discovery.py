from __future__ import annotations

import httpx
import pytest
import respx

from aiofhir.errors import DiscoveryError
from aiofhir.profiles import GenericProfile, HasteProfile
from aiofhir.smart.discovery import discover

BASE = "http://h.example/w/my-health/lk-demo/api/v1/fhir"
PRIMARY = f"{BASE}/.well-known/smart-configuration"
FALLBACK = "http://h.example/w/my-health/lk-demo/api/v1/.well-known/smart-configuration"

CONFIG = {
    "authorization_endpoint": "http://h.example/authorize",
    "token_endpoint": "http://h.example/token",
    "code_challenge_methods_supported": ["S256"],
    "capabilities": ["permission-patient", "permission-v2"],
    "scopes_supported": ["patient/*.rs"],
}


def test_haste_discovery_paths_include_fallback() -> None:
    paths = HasteProfile().discovery_paths(BASE + "/")
    assert paths == [PRIMARY, FALLBACK]


def test_generic_discovery_path_is_standard() -> None:
    assert GenericProfile().discovery_paths("http://fhir.example/r4/") == [
        "http://fhir.example/r4/.well-known/smart-configuration"
    ]


async def test_discovery_uses_fallback_when_primary_is_missing() -> None:
    with respx.mock:
        respx.get(PRIMARY).respond(404)
        fallback = respx.get(FALLBACK).respond(200, json=CONFIG)
        found = await discover(BASE, HasteProfile(), httpx.AsyncClient())
    assert found.authorization_endpoint == "http://h.example/authorize"
    assert fallback.called


async def test_discovery_does_not_call_fallback_when_primary_succeeds() -> None:
    with respx.mock:
        respx.get(PRIMARY).respond(200, json=CONFIG)
        fallback = respx.get(FALLBACK).respond(200, json=CONFIG)
        await discover(BASE, HasteProfile(), httpx.AsyncClient())
    assert not fallback.called


async def test_discovery_is_cached_per_base() -> None:
    with respx.mock:
        route = respx.get(PRIMARY).respond(200, json=CONFIG)
        client = httpx.AsyncClient()
        await discover(BASE, HasteProfile(), client)
        await discover(BASE, HasteProfile(), client)
    assert route.call_count == 1


async def test_discovery_fails_when_every_path_fails() -> None:
    with respx.mock:
        respx.get(PRIMARY).respond(404)
        respx.get(FALLBACK).respond(404)
        with pytest.raises(DiscoveryError):
            await discover(BASE, HasteProfile(), httpx.AsyncClient())


async def test_discovery_requires_s256() -> None:
    body = {**CONFIG, "code_challenge_methods_supported": ["plain"]}
    with respx.mock:
        respx.get(PRIMARY).respond(200, json=body)
        with pytest.raises(DiscoveryError):
            await discover(BASE, HasteProfile(), httpx.AsyncClient())
