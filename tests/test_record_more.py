"""Phase 5 helpers: immunizations, vital signs, past visits, procedures, documents."""

from __future__ import annotations

from datetime import UTC, date, datetime

import respx

from aiofhir import FhirClient, PatientRecord

BASE = "http://fhir.example/r4"
WHEN = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class FakeTokens:
    async def get(self) -> str:
        return "token"

    async def refresh(self, stale: str | None = None) -> str:
        return "token"


async def _no_sleep(_: float) -> None:
    return None


def _bundle(resources: list[dict[str, object]]) -> dict[str, object]:
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}


def _record(fhir: FhirClient) -> PatientRecord:
    return PatientRecord(fhir, patient_id="pt-1", clock=lambda: WHEN)


async def test_immunizations_keep_completed_newest_first() -> None:
    with respx.mock:
        route = respx.get(f"{BASE}/Immunization").respond(
            200,
            json=_bundle(
                [
                    {
                        "resourceType": "Immunization",
                        "id": "flu-2024",
                        "status": "completed",
                        "vaccineCode": {"text": "Influenza"},
                        "occurrenceDateTime": "2024-10-01",
                    },
                    {
                        "resourceType": "Immunization",
                        "id": "flu-2025",
                        "status": "completed",
                        "vaccineCode": {"text": "Influenza"},
                        "occurrenceDateTime": "2025-10-03T09:00:00Z",
                    },
                    {
                        "resourceType": "Immunization",
                        "id": "skipped",
                        "status": "not-done",
                        "vaccineCode": {"text": "Tdap"},
                        "occurrenceDateTime": "2025-11-01",
                    },
                ]
            ),
        )
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            summary = await _record(fhir).immunizations()
    params = route.calls.last.request.url.params
    assert params["patient"] == "pt-1" and params["status"] == "completed"
    assert summary.kind == "immunizations"
    assert [item.citation.id for item in summary.items] == ["flu-2025", "flu-2024"]
    assert summary.items[0].text == "Influenza"
    assert summary.items[1].at == date(2024, 10, 1)


async def test_vital_signs_read_blood_pressure_components_in_order() -> None:
    blood_pressure: dict[str, object] = {
        "resourceType": "Observation",
        "id": "bp",
        "code": {"text": "Blood pressure"},
        "effectiveDateTime": "2026-05-20T10:00:00Z",
        "component": [
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]},
                "valueQuantity": {"value": 80, "unit": "mmHg"},
            },
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                "valueQuantity": {"value": 120, "unit": "mmHg"},
            },
        ],
    }
    weight: dict[str, object] = {
        "resourceType": "Observation",
        "id": "weight",
        "code": {"text": "Body weight"},
        "effectiveDateTime": "2026-05-25T10:00:00Z",
        "valueQuantity": {"value": 70.5, "unit": "kg"},
    }
    with respx.mock:
        route = respx.get(f"{BASE}/Observation").respond(
            200, json=_bundle([blood_pressure, weight])
        )
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            summary = await _record(fhir).vital_signs(days=30)
    params = route.calls.last.request.url.params
    assert params["category"] == "vital-signs" and params["patient"] == "pt-1"
    assert params["date"] == "ge2026-05-02T12:00:00Z"
    assert [item.text for item in summary.items] == [
        "Body weight: 70.5 kg",
        "Blood pressure: 120/80 mmHg",
    ]


async def test_past_visits_keep_finished_encounters() -> None:
    with respx.mock:
        route = respx.get(f"{BASE}/Encounter").respond(
            200,
            json=_bundle(
                [
                    {
                        "resourceType": "Encounter",
                        "id": "checkup",
                        "status": "finished",
                        "class": {"code": "AMB"},
                        "type": [{"text": "General examination"}],
                        "period": {"start": "2026-03-01T14:00:00Z"},
                    },
                    {
                        "resourceType": "Encounter",
                        "id": "er",
                        "status": "finished",
                        "class": {"code": "EMER"},
                        "period": {"start": "2026-04-10T02:00:00Z"},
                    },
                    {
                        "resourceType": "Encounter",
                        "id": "planned",
                        "status": "planned",
                        "type": [{"text": "Follow up"}],
                        "period": {"start": "2026-07-01T14:00:00Z"},
                    },
                ]
            ),
        )
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            summary = await _record(fhir).past_visits()
    params = route.calls.last.request.url.params
    assert params["status"] == "finished" and params["date"] == "ge2025-06-01T12:00:00Z"
    assert [(i.citation.id, i.text) for i in summary.items] == [
        ("er", "EMER"),
        ("checkup", "General examination"),
    ]


async def test_procedures_keep_completed_and_read_period_starts() -> None:
    with respx.mock:
        respx.get(f"{BASE}/Procedure").respond(
            200,
            json=_bundle(
                [
                    {
                        "resourceType": "Procedure",
                        "id": "colonoscopy",
                        "status": "completed",
                        "code": {"text": "Colonoscopy"},
                        "performedPeriod": {"start": "2026-01-15T08:00:00Z"},
                    },
                    {
                        "resourceType": "Procedure",
                        "id": "in-progress",
                        "status": "in-progress",
                        "code": {"text": "Physical therapy"},
                        "performedDateTime": "2026-05-01",
                    },
                ]
            ),
        )
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            summary = await _record(fhir).procedures(days=365)
    assert [item.text for item in summary.items] == ["Colonoscopy"]
    assert summary.items[0].at == datetime(2026, 1, 15, 8, 0, tzinfo=UTC)


async def test_documents_give_titles_and_dates_but_never_content() -> None:
    secret_note = "Patient disclosed sensitive history"
    with respx.mock:
        route = respx.get(f"{BASE}/DocumentReference").respond(
            200,
            json=_bundle(
                [
                    {
                        "resourceType": "DocumentReference",
                        "id": "visit-note",
                        "status": "current",
                        "type": {"text": "Progress note"},
                        "date": "2026-05-02T16:00:00Z",
                        "content": [
                            {"attachment": {"contentType": "text/plain", "data": "UGF0aWVudA=="}}
                        ],
                        "text": {"div": f"<div>{secret_note}</div>"},
                    },
                    {
                        "resourceType": "DocumentReference",
                        "id": "discharge",
                        "status": "current",
                        "description": "Discharge summary",
                        "type": {"text": "Summary"},
                        "date": "2026-05-20",
                    },
                    {
                        "resourceType": "DocumentReference",
                        "id": "old",
                        "status": "superseded",
                        "description": "Old note",
                        "date": "2026-05-21",
                    },
                ]
            ),
        )
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            summary = await _record(fhir).documents()
    params = route.calls.last.request.url.params
    assert params["status"] == "current"
    assert [item.text for item in summary.items] == ["Discharge summary", "Progress note"]
    rendered = summary.to_text()
    assert secret_note not in rendered and "UGF0aWVudA" not in rendered


async def test_new_helpers_only_search_their_own_resource_type() -> None:
    with respx.mock:
        respx.get(f"{BASE}/Immunization").respond(
            200,
            json=_bundle(
                [
                    {"resourceType": "Patient", "id": "someone-else"},
                    {
                        "resourceType": "Immunization",
                        "id": "flu",
                        "status": "completed",
                        "vaccineCode": {"text": "Influenza"},
                    },
                ]
            ),
        )
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            summary = await _record(fhir).immunizations()
    assert [item.citation.id for item in summary.items] == ["flu"]


def test_whole_number_decimals_print_without_a_trailing_zero() -> None:
    from aiofhir.record import _vital_text

    blood_pressure: dict[str, object] = {
        "code": {"text": "Blood pressure"},
        "component": [
            {
                "code": {"coding": [{"code": "8462-4"}]},
                "valueQuantity": {"value": 78.0, "unit": "mmHg"},
            },
            {
                "code": {"coding": [{"code": "8480-6"}]},
                "valueQuantity": {"value": 118.0, "unit": "mmHg"},
            },
        ],
    }
    lab: dict[str, object] = {"code": {"text": "A1c"}, "valueQuantity": {"value": 6.0, "unit": "%"}}
    weight: dict[str, object] = {
        "code": {"text": "Weight"},
        "valueQuantity": {"value": 70.2, "unit": "kg"},
    }
    assert _vital_text(blood_pressure) == "Blood pressure: 118/78 mmHg"
    assert _vital_text(lab) == "A1c: 6 %"
    assert _vital_text(weight) == "Weight: 70.2 kg"
