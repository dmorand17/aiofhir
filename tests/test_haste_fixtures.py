"""Replay recorded, de-identified Haste responses through the helpers."""

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


async def test_helpers_summarize_the_recorded_haste_record() -> None:
    with respx.mock:
        for resource in ("Appointment", "MedicationRequest", "AllergyIntolerance", "Condition"):
            respx.get(f"{BASE}/{resource}").respond(
                200, json=_load(f"{resource.lower()}-search.json")
            )
        respx.get(f"{BASE}/Observation").respond(200, json=_load("observation-search.json"))
        respx.get(f"{BASE}/Medication/med-remote").respond(
            200, json=_load("medication-med-remote.json")
        )
        async with FhirClient(BASE, _Tokens(), profile=HasteProfile()) as fhir:
            record = PatientRecord(fhir, patient_id=PATIENT)
            upcoming = await record.upcoming_appointments()
            meds = await record.active_medications()
            allergies = await record.allergies()
            conditions = await record.conditions()
            labs = await record.recent_labs()
    # The recorded Bundle also holds the past (fulfilled) and cancelled visits.
    assert [item.text for item in upcoming.items] == [
        "appt-booked-3",
        "appt-pending",
        "appt-booked-14",
        "appt-booked-45",
    ]
    assert sorted(item.text for item in meds.items) == ["Aspirin", "Lisinopril", "Metformin"]
    assert [item.text for item in allergies.items] == ["Peanut"]
    assert [item.text for item in conditions.items] == ["Asthma"]
    assert [item.text for item in labs.items] == ["A1c: 6.1 %"]
    for summary in (upcoming, meds, allergies, conditions, labs):
        assert all(item.citation.id for item in summary.items)
