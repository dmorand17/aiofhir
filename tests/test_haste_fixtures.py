"""Replay recorded, de-identified Haste responses through discovery and search_raw."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import respx

from aiofhir import FhirClient, PatientRecord
from aiofhir.profiles import HasteProfile
from aiofhir.smart.discovery import clear_discovery_cache, discover
from aiofhir.smart.scopes import scopes_from_capabilities

FIXTURES = Path(__file__).parent / "fixtures" / "haste"
BASE = "https://api.haste.health/w/livekit/synthea-sandbox/api/v1/fhir"
PATIENT = "example-patient"
_STATEMENT = json.loads(
    (Path(__file__).parent / "fixtures" / "capability-statement.json").read_text()
)


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class _Tokens:
    async def get(self) -> str:
        return "token"

    async def refresh(self, stale: str | None = None) -> str:
        return "token"


async def test_recorded_configuration_derives_the_scope_haste_accepted() -> None:
    clear_discovery_cache()
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200, json=_load("smart-configuration.json")
        )
        async with httpx.AsyncClient() as http:
            configuration = await discover(BASE, HasteProfile(), http)
    clear_discovery_cache()
    assert scopes_from_capabilities(configuration) == (
        "patient/*.rs launch/patient openid fhirUser offline_access"
    )


async def test_search_raw_replays_the_recorded_haste_record() -> None:
    searches = {
        "Appointment": "appointment-search.json",
        "MedicationRequest": "medicationrequest-search.json",
        "AllergyIntolerance": "allergyintolerance-search.json",
        "Condition": "condition-search.json",
        "Immunization": "immunization-search.json",
        "Encounter": "encounter-search.json",
        "Procedure": "procedure-search.json",
        "DocumentReference": "documentreference-search.json",
        "Observation": "observation-search.json",
    }
    with respx.mock:
        respx.get(f"{BASE}/metadata").respond(200, json=_STATEMENT)
        routes = {
            resource: respx.get(f"{BASE}/{resource}").respond(200, json=_load(name))
            for resource, name in searches.items()
        }
        async with FhirClient(BASE, _Tokens(), profile=HasteProfile()) as fhir:
            record = PatientRecord(fhir, patient_id=PATIENT)
            results = {resource: await record.search_raw(resource) for resource in searches}
    for resource, result in results.items():
        assert result.resources, resource
        assert {item["resourceType"] for item in result.resources} == {resource}
        params = routes[resource].calls.last.request.url.params
        assert params.get_list("patient") == [PATIENT]
