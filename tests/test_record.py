from __future__ import annotations

from datetime import UTC, datetime

import pytest
import respx

from aiofhir import FhirClient, PatientRecord

BASE = "http://fhir.example/r4"
WHEN = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class FakeTokens:
    async def get(self) -> str:
        return "token"

    async def refresh(self) -> str:
        return "token"


def _bundle(resources: list[dict[str, object]]) -> dict[str, object]:
    return {"resourceType": "Bundle", "entry": [{"resource": resource} for resource in resources]}


async def _record() -> tuple[FhirClient, PatientRecord]:
    fhir = FhirClient(BASE, FakeTokens(), sleep=_no_sleep)
    return fhir, PatientRecord(fhir, patient_id="pt-1", clock=lambda: WHEN)


async def test_upcoming_appointments_filters_sorts_and_forces_patient() -> None:
    with respx.mock:
        route = respx.get(f"{BASE}/Appointment").respond(
            200,
            json=_bundle(
                [
                    {
                        "resourceType": "Appointment",
                        "id": "later",
                        "status": "pending",
                        "start": "2026-06-15T15:00:00Z",
                        "description": "Follow up",
                    },
                    {
                        "resourceType": "Appointment",
                        "id": "soon",
                        "status": "booked",
                        "start": "2026-06-04T15:00:00Z",
                        "description": "Office visit",
                    },
                    {
                        "resourceType": "Appointment",
                        "id": "cancelled",
                        "status": "cancelled",
                        "start": "2026-06-05T15:00:00Z",
                        "description": "Cancelled visit",
                    },
                    {
                        "resourceType": "Appointment",
                        "id": "proposed",
                        "status": "proposed",
                        "start": "2026-06-06T15:00:00Z",
                        "description": "Proposed visit",
                    },
                ]
            ),
        )
        fhir, record = await _record()
        async with fhir:
            summary = await record.upcoming_appointments(days=90)
            with pytest.raises(TypeError):
                await record.upcoming_appointments(patient="other")  # type: ignore[call-arg]
    request = route.calls.last.request
    assert request.url.params.get_list("patient") == ["pt-1"]
    assert request.url.params.get_list("date") == [
        "ge2026-06-01T12:00:00Z",
        "lt2026-08-30T12:00:00Z",
    ]
    assert request.url.params.get_list("_sort") == ["date"]
    assert [item.citation.id for item in summary.items] == ["soon", "later"]
    assert [item.text for item in summary.items] == ["Office visit", "Follow up"]


async def test_search_raw_refuses_resources_outside_the_allowlist() -> None:
    fhir, record = await _record()
    async with fhir:
        with pytest.raises(ValueError):
            await record.search_raw("Account")


async def test_search_raw_replaces_a_caller_supplied_patient() -> None:
    with respx.mock:
        route = respx.get(f"{BASE}/Observation").respond(200, json=_bundle([]))
        fhir, record = await _record()
        async with fhir:
            await record.search_raw(
                "Observation",
                {"patient": "someone-else", "category": "laboratory"},
            )
    assert route.calls.last.request.url.params.get_list("patient") == ["pt-1"]


async def test_active_medications_resolve_inline_contained_and_referenced() -> None:
    with respx.mock:
        respx.get(f"{BASE}/MedicationRequest").respond(
            200,
            json=_bundle(
                [
                    {
                        "resourceType": "MedicationRequest",
                        "id": "inline",
                        "status": "active",
                        "medicationCodeableConcept": {"text": "Lisinopril"},
                    },
                    {
                        "resourceType": "MedicationRequest",
                        "id": "contained",
                        "status": "active",
                        "contained": [
                            {
                                "resourceType": "Medication",
                                "id": "med1",
                                "code": {"text": "Aspirin"},
                            }
                        ],
                        "medicationReference": {"reference": "#med1"},
                    },
                    {
                        "resourceType": "MedicationRequest",
                        "id": "remote",
                        "status": "active",
                        "medicationReference": {"reference": "Medication/med-9"},
                    },
                    {
                        "resourceType": "MedicationRequest",
                        "id": "stopped",
                        "status": "stopped",
                        "medicationCodeableConcept": {"text": "Stopped drug"},
                    },
                ]
            ),
        )
        respx.get(f"{BASE}/Medication/med-9").respond(
            200,
            json={"resourceType": "Medication", "id": "med-9", "code": {"text": "Metformin"}},
        )
        fhir, record = await _record()
        async with fhir:
            summary = await record.active_medications()
    assert [item.text for item in summary.items] == ["Lisinopril", "Aspirin", "Metformin"]
    assert [item.citation.id for item in summary.items] == ["inline", "contained", "remote"]


async def test_conditions_allergies_and_labs_keep_active_matching_records() -> None:
    with respx.mock:
        respx.get(f"{BASE}/AllergyIntolerance").respond(
            200,
            json=_bundle(
                [
                    {
                        "resourceType": "AllergyIntolerance",
                        "id": "peanut",
                        "clinicalStatus": {"coding": [{"code": "active"}]},
                        "code": {"text": "Peanut"},
                    },
                    {
                        "resourceType": "AllergyIntolerance",
                        "id": "old",
                        "clinicalStatus": {"coding": [{"code": "inactive"}]},
                        "code": {"text": "Dust"},
                    },
                ]
            ),
        )
        respx.get(f"{BASE}/Condition").respond(
            200,
            json=_bundle(
                [
                    {
                        "resourceType": "Condition",
                        "id": "asthma",
                        "clinicalStatus": {"coding": [{"code": "active"}]},
                        "category": [{"coding": [{"code": "problem-list-item"}]}],
                        "code": {"text": "Asthma"},
                    },
                    {
                        "resourceType": "Condition",
                        "id": "encounter-dx",
                        "clinicalStatus": {"coding": [{"code": "active"}]},
                        "category": [{"coding": [{"code": "encounter-diagnosis"}]}],
                        "code": {"text": "Cough"},
                    },
                ]
            ),
        )
        labs_route = respx.get(f"{BASE}/Observation").respond(
            200,
            json=_bundle(
                [
                    {
                        "resourceType": "Observation",
                        "id": "old-lab",
                        "effectiveDateTime": "2026-05-01T00:00:00Z",
                        "code": {"text": "Glucose"},
                    },
                    {
                        "resourceType": "Observation",
                        "id": "new-lab",
                        "effectiveDateTime": "2026-05-20T00:00:00Z",
                        "code": {"text": "A1c"},
                    },
                ]
            ),
        )
        fhir, record = await _record()
        async with fhir:
            allergies = await record.allergies()
            conditions = await record.conditions()
            labs = await record.recent_labs(days=90)
    assert [item.text for item in allergies.items] == ["Peanut"]
    assert [item.citation.id for item in conditions.items] == ["asthma"]
    assert [item.citation.id for item in labs.items] == ["new-lab", "old-lab"]
    lab_request = labs_route.calls.last.request
    assert lab_request.url.params.get_list("category") == ["laboratory"]
    assert lab_request.url.params.get_list("patient") == ["pt-1"]
    assert lab_request.url.params.get_list("date") == ["ge2026-03-03T12:00:00Z"]


async def _no_sleep(_seconds: float) -> None:
    return None
