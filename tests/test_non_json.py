"""Bodies that are not JSON raise library errors, not JSONDecodeError."""

from __future__ import annotations

import httpx
import pytest
import respx

from aiofhir import FhirClient
from aiofhir.errors import DiscoveryError, FhirError
from aiofhir.profiles import GenericProfile, HasteProfile
from aiofhir.smart.discovery import clear_discovery_cache, discover

BASE = "http://fhir.example/r4"
HTML = "<html><body>Proxy login</body></html>"


class _Tokens:
    async def get(self) -> str:
        return "tok"

    async def refresh(self, stale: str | None = None) -> str:
        return "tok"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    clear_discovery_cache()


async def test_read_with_html_body_raises_fhir_error() -> None:
    with respx.mock:
        respx.get(f"{BASE}/Patient/p1").respond(200, text=HTML)
        async with FhirClient(BASE, _Tokens()) as fhir:
            with pytest.raises(FhirError, match="not JSON") as caught:
                await fhir.read("Patient", "p1")
    assert caught.value.status == 200


async def test_vread_and_capabilities_with_html_body_raise_fhir_error() -> None:
    with respx.mock:
        respx.get(f"{BASE}/Patient/p1/_history/1").respond(200, text=HTML)
        respx.get(f"{BASE}/metadata").respond(200, text=HTML)
        async with FhirClient(BASE, _Tokens()) as fhir:
            with pytest.raises(FhirError):
                await fhir.vread("Patient", "p1", "1")
            with pytest.raises(FhirError):
                await fhir.capabilities()


async def test_search_with_html_body_raises_fhir_error() -> None:
    with respx.mock:
        respx.get(f"{BASE}/Observation").respond(200, text=HTML)
        async with FhirClient(BASE, _Tokens()) as fhir:
            with pytest.raises(FhirError, match="not JSON"):
                await fhir.search("Observation", {"category": "laboratory"})
            with pytest.raises(FhirError):
                async for _ in fhir.iter_search("Observation"):
                    pass


async def test_error_status_with_html_body_still_raises_fhir_error() -> None:
    with respx.mock:
        respx.get(f"{BASE}/Patient/p1").respond(404, text=HTML)
        async with FhirClient(BASE, _Tokens()) as fhir:
            with pytest.raises(FhirError) as caught:
                await fhir.read("Patient", "p1")
    assert caught.value.status == 404


async def test_discovery_html_body_raises_discovery_error() -> None:
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, text=HTML)
        with pytest.raises(DiscoveryError):
            await discover(BASE, GenericProfile(), httpx.AsyncClient())


async def test_discovery_html_on_primary_falls_back() -> None:
    base = "http://h.example/w/t/p/api/v1/fhir"
    config = {
        "authorization_endpoint": "http://h.example/authorize",
        "token_endpoint": "http://h.example/token",
        "code_challenge_methods_supported": ["S256"],
        "capabilities": ["permission-patient"],
    }
    with respx.mock:
        respx.get(f"{base}/.well-known/smart-configuration").respond(200, text=HTML)
        respx.get("http://h.example/w/t/p/api/v1/.well-known/smart-configuration").respond(
            200, json=config
        )
        found = await discover(base, HasteProfile(), httpx.AsyncClient())
    assert found.token_endpoint == "http://h.example/token"
