"""0.1.1 hardening: findings from the code reviews."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from aiofhir import FhirClient, PatientRecord
from aiofhir.errors import DiscoveryError, TokenError
from aiofhir.profiles import GenericProfile, HasteProfile
from aiofhir.smart import SmartClient
from aiofhir.smart import discovery as discovery_module
from aiofhir.smart.discovery import clear_discovery_cache, discover

BASE = "http://fhir.example/w/acme/prod/api/v1/fhir"
WHEN = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class FakeTokens:
    async def get(self) -> str:
        return "token"

    async def refresh(self, stale: str | None = None) -> str:
        return "token"


async def _no_sleep(_: float) -> None:
    return None


def _bundle(resources: list[dict[str, object]], nxt: str | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "resourceType": "Bundle",
        "entry": [{"resource": r} for r in resources],
    }
    if nxt:
        body["link"] = [{"relation": "next", "url": nxt}]
    return body


@pytest.fixture(autouse=True)
def _fresh_discovery() -> None:
    clear_discovery_cache()


# Percent-encoded dot segments in next links


@pytest.mark.parametrize(
    "path",
    [
        "/w/acme/prod/api/v1/fhir/%2e%2e/%2e%2e/%2e%2e/other/api/v1/fhir/Observation",
        "/w/acme/prod/api/v1/fhir/%2E%2E/x",
        "/w/acme/prod/api/v1/fhir/..%2f..%2fother",
        "/w/acme/prod/api/v1/fhir/%2e/Observation",
    ],
)
async def test_encoded_dot_segments_in_next_links_are_not_followed(path: str) -> None:
    with respx.mock:
        respx.get(f"{BASE}/Observation").respond(
            200, json=_bundle([], nxt=f"http://fhir.example{path}")
        )
        escaped = respx.get(url__regex=r"http://fhir\.example/.*(%2e|%2E|\.\.).*").respond(
            200, json=_bundle([])
        )
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            result = await fhir.search("Observation")
    assert not escaped.called
    assert any("outside the FHIR base" in w for w in result.warnings)


# L1: upcoming appointments outside the window


async def test_upcoming_appointments_drop_starts_outside_the_window() -> None:
    def appointment(appointment_id: str, start: str | None) -> dict[str, object]:
        resource: dict[str, object] = {
            "resourceType": "Appointment",
            "id": appointment_id,
            "status": "booked",
            "description": appointment_id,
        }
        if start:
            resource["start"] = start
        return resource

    with respx.mock:
        respx.get(f"{BASE}/Appointment").respond(
            200,
            json=_bundle(
                [
                    appointment("past", "2026-05-01T10:00:00Z"),
                    appointment("soon", "2026-06-03T10:00:00Z"),
                    appointment("too-far", "2026-12-01T10:00:00Z"),
                    appointment("no-start", None),
                ]
            ),
        )
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            record = PatientRecord(fhir, patient_id="pt-1", clock=lambda: WHEN)
            summary = await record.upcoming_appointments(days=30)
    assert [item.citation.id for item in summary.items] == ["soon", "no-start"]


# C5: re-entering an open client


async def test_entering_an_open_client_again_is_refused() -> None:
    fhir = FhirClient(BASE, FakeTokens())
    async with fhir:
        with pytest.raises(RuntimeError, match="already open"):
            await fhir.__aenter__()


# C2: token and discovery requests


async def test_a_token_endpoint_transport_failure_is_a_token_error() -> None:
    smart = SmartClient(BASE, "app", redirect_uri="https://app/cb", timeout=2.0)
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200,
            json={
                "authorization_endpoint": "http://auth.example/authorize",
                "token_endpoint": "http://auth.example/token",
                "code_challenge_methods_supported": ["S256"],
                "capabilities": ["permission-patient"],
            },
        )
        respx.post("http://auth.example/token").mock(side_effect=httpx.ConnectTimeout("slow"))
        with pytest.raises(TokenError, match="token request failed"):
            await smart.exchange_code("code", "verifier")


# C3: discovery cache


def _config() -> dict[str, object]:
    return {
        "authorization_endpoint": "http://auth.example/authorize",
        "token_endpoint": "http://auth.example/token",
        "code_challenge_methods_supported": ["S256"],
        "capabilities": ["permission-patient"],
    }


async def test_the_discovery_cache_is_keyed_by_profile_too() -> None:
    with respx.mock:
        primary = respx.get(f"{BASE}/.well-known/smart-configuration").respond(404)
        fallback = respx.get(
            "http://fhir.example/w/acme/prod/api/v1/.well-known/smart-configuration"
        ).respond(200, json=_config())
        async with httpx.AsyncClient() as http:
            await discover(BASE, HasteProfile(), http)  # cached via Haste's fallback path
            with pytest.raises(DiscoveryError):  # the generic profile has no fallback
                await discover(BASE, GenericProfile(), http)
    assert primary.call_count == 2 and fallback.call_count == 1


async def test_the_discovery_cache_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(discovery_module, "_monotonic", lambda: now[0])
    with respx.mock:
        route = respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        async with httpx.AsyncClient() as http:
            await discover(BASE, GenericProfile(), http)
            await discover(BASE, GenericProfile(), http)
            now[0] += 3601
            await discover(BASE, GenericProfile(), http)
    assert route.call_count == 2


# C4: medication reads


async def test_a_failed_medication_read_keeps_the_rest_of_the_summary() -> None:
    def request(request_id: str, medication: str) -> dict[str, object]:
        return {
            "resourceType": "MedicationRequest",
            "id": request_id,
            "status": "active",
            "medicationReference": {"reference": f"Medication/{medication}"},
        }

    with respx.mock:
        respx.get(f"{BASE}/MedicationRequest").respond(
            200, json=_bundle([request("mr-ok", "med-ok"), request("mr-gone", "med-gone")])
        )
        respx.get(f"{BASE}/Medication/med-ok").respond(
            200, json={"resourceType": "Medication", "id": "med-ok", "code": {"text": "Aspirin"}}
        )
        respx.get(f"{BASE}/Medication/med-gone").respond(404)
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            record = PatientRecord(fhir, patient_id="pt-1", clock=lambda: WHEN)
            summary = await record.active_medications()
    assert [(i.citation.id, i.text) for i in summary.items] == [
        ("mr-ok", "Aspirin"),
        ("mr-gone", ""),
    ]
    assert any("MedicationRequest/mr-gone" in w for w in summary.warnings)


# M1: repeated resources across pages


async def test_resources_repeated_across_pages_are_dropped() -> None:
    def observation(observation_id: str) -> dict[str, object]:
        return {"resourceType": "Observation", "id": observation_id}

    with respx.mock:
        respx.get(f"{BASE}/Observation", params={"page": "2"}).respond(
            200, json=_bundle([observation("o2"), observation("o3")])
        )
        respx.get(f"{BASE}/Observation").respond(
            200,
            json=_bundle([observation("o1"), observation("o2")], nxt=f"{BASE}/Observation?page=2"),
        )
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            result = await fhir.search("Observation")
    assert [r["id"] for r in result.resources] == ["o1", "o2", "o3"]
    assert any("repeated" in w for w in result.warnings)
