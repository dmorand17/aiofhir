"""Opt-in checks against a live Haste project.

Set AIOFHIR_LIVE=1, BASE, HASTE_ACCESS_TOKEN, and PATIENT_ID. These are
skipped by the default test run.
"""

from __future__ import annotations

import os

import pytest

from aiofhir import FhirClient, PatientRecord

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOFHIR_LIVE") != "1",
    reason="Set AIOFHIR_LIVE=1 to run the live Haste checks.",
)


class _Token:
    def __init__(self, token: str) -> None:
        self._token = token

    async def get(self) -> str:
        return self._token

    async def refresh(self, stale: str | None = None) -> str:
        return self._token


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"AIOFHIR_LIVE=1 requires {name}")
    return value


async def test_live_record_stays_on_the_token_patient() -> None:
    base = _required("BASE")
    patient_id = _required("PATIENT_ID")
    token = _required("HASTE_ACCESS_TOKEN")
    async with FhirClient(base, _Token(token)) as fhir:
        record = PatientRecord(fhir, patient_id=patient_id)
        summary = await record.upcoming_appointments()
        labs = await fhir.search("Observation", {"patient": patient_id, "category": "laboratory"})
    assert summary.kind == "upcoming_appointments"
    for item in summary.items:
        assert item.citation.resource_type == "Appointment"
        assert item.citation.id
    for resource in labs.resources:
        subject = resource.get("subject")
        reference = subject.get("reference") if isinstance(subject, dict) else ""
        assert patient_id in str(reference)
    assert labs.truncated in (True, False)
