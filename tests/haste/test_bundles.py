"""The hand-posted Haste bundles."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import respx

from aiofhir import FhirClient, PatientRecord
from haste.policy_model import decide

BUNDLES = Path(__file__).parents[2] / "haste" / "bundles"


def test_the_agent_client_bundle_never_holds_a_real_secret() -> None:
    bundle = json.loads((BUNDLES / "create-agent-client.json").read_text())
    [entry] = bundle["entry"]
    client = entry["resource"]
    assert client["secret"] == "REPLACE_WITH_A_NEW_SECRET"
    assert entry["request"] == {"method": "PUT", "url": f"ClientApplication/{client['id']}"}
    assert "http://localhost:8000/auth/callback" in client["redirectUri"]
    assert "patient/*.rs" in client["scope"].split()


def _walter() -> dict[str, dict[str, Any]]:
    bundle = json.loads((BUNDLES / "create-walter-white.json").read_text())
    return {
        f"{e['resource']['resourceType']}/{e['resource']['id']}": e["resource"]
        for e in bundle["entry"]
    }


def test_the_walter_bundle_has_its_own_policy_and_a_user_placeholder() -> None:
    resources = _walter()
    text = (BUNDLES / "create-walter-white.json").read_text()
    assert "aiofhir-seed-patient-read" not in text  # never the seed patient's policy
    membership = resources["Membership/walter-white-membership"]
    assert membership["user"] == {"reference": "User/REPLACE_WITH_USER_ID"}
    assert membership["link"] == {"reference": "Patient/walter-white"}
    assignment = resources["AccessPolicyV2Assignment/walter-white-read-assignment"]
    assert assignment["accessPolicy"] == {"reference": "AccessPolicyV2/walter-white-read"}
    assert assignment["link"] == {"reference": "Membership/walter-white-membership"}


def test_walters_policy_reads_only_walter() -> None:
    resources = _walter()
    policy = resources["AccessPolicyV2/walter-white-read"]
    store = {
        **resources,
        "Patient/someone-else": {"resourceType": "Patient", "id": "someone-else"},
        "MedicationRequest/theirs": {
            "resourceType": "MedicationRequest",
            "id": "theirs",
            "subject": {"reference": "Patient/someone-else"},
        },
    }
    assert decide(policy, "GET", "Patient/walter-white", store)
    assert decide(policy, "GET", "MedicationRequest/ww-mr-metformin", store)
    assert decide(policy, "GET", "Medication/ww-med-atorvastatin", store)
    assert not decide(policy, "GET", "Patient/someone-else", store)
    assert not decide(policy, "GET", "MedicationRequest/theirs", store)
    assert not decide(policy, "DELETE", "MedicationRequest/ww-mr-metformin", store)


async def test_walters_record_through_the_helpers() -> None:
    resources = _walter()
    base = "https://fhir.example/r4"

    def bundle_of(kind: str) -> dict[str, object]:
        entries = [{"resource": r} for k, r in resources.items() if k.startswith(f"{kind}/")]
        return {"resourceType": "Bundle", "entry": entries}

    class Tokens:
        async def get(self) -> str:
            return "t"

        async def refresh(self, stale: str | None = None) -> str:
            return "t"

    with respx.mock:
        respx.get(f"{base}/MedicationRequest").respond(200, json=bundle_of("MedicationRequest"))
        respx.get(f"{base}/Appointment").respond(200, json=bundle_of("Appointment"))
        respx.get(f"{base}/Medication/ww-med-atorvastatin").respond(
            200, json=resources["Medication/ww-med-atorvastatin"]
        )
        async with FhirClient(base, Tokens()) as fhir:
            record = PatientRecord(
                fhir, patient_id="walter-white", clock=lambda: datetime(2026, 11, 28, tzinfo=UTC)
            )
            meds = await record.active_medications()
            upcoming = await record.upcoming_appointments()
    assert sorted(item.text for item in meds.items) == [
        "Atorvastatin 20 mg tablet: Take 1 tablet at bedtime",
        "Lisinopril 10 mg tablet: Take 1 tablet once daily",
        "Metformin 500 mg tablet: Take 1 tablet twice daily with meals",
    ]
    assert [item.text for item in upcoming.items] == [
        "Primary care follow-up",
        "Fasting lab work",
        "Pulmonology consult",
    ]
