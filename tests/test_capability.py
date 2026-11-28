from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from aiofhir import FhirClient, PatientRecord
from aiofhir.capability import fetch_capability_statement, patient_search_types, readable_types
from aiofhir.errors import DiscoveryError
from aiofhir.smart import SmartClient

BASE = "http://fhir.example/r4"
STATEMENT = json.loads(
    (Path(__file__).parent / "fixtures" / "capability-statement.json").read_text()
)


class _Tokens:
    async def get(self) -> str:
        return "token"

    async def refresh(self, stale: str | None = None) -> str:
        return "token"


def test_readable_types_keep_read_or_search_in_statement_order() -> None:
    assert readable_types(STATEMENT) == (
        "Patient",
        "Appointment",
        "MedicationRequest",
        "AllergyIntolerance",
        "Condition",
        "Observation",
        "Immunization",
        "Encounter",
        "Procedure",
        "DocumentReference",
        "Medication",
        "Practitioner",
    )


def test_patient_search_types_need_a_patient_param_or_are_patient() -> None:
    found = patient_search_types(STATEMENT)
    assert "Patient" in found and "Observation" in found
    assert "Medication" not in found  # searchable, but not by patient
    assert "Practitioner" not in found  # read only


def test_only_server_mode_rest_counts() -> None:
    client_only = {
        "resourceType": "CapabilityStatement",
        "rest": [
            {"mode": "client", "resource": [{"type": "Patient", "interaction": [{"code": "read"}]}]}
        ],
    }
    assert readable_types(client_only) == ()
    assert patient_search_types(client_only) == frozenset()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"resourceType": "OperationOutcome"}),
    ],
)
async def test_an_unreadable_statement_is_a_discovery_error(response: httpx.Response) -> None:
    with respx.mock:
        respx.get(f"{BASE}/metadata").mock(return_value=response)
        async with httpx.AsyncClient() as http:
            with pytest.raises(DiscoveryError):
                await fetch_capability_statement(BASE, http)


async def test_search_raw_refuses_a_type_the_server_cannot_search_by_patient() -> None:
    with respx.mock:
        respx.get(f"{BASE}/metadata").respond(200, json=STATEMENT)
        async with FhirClient(BASE, _Tokens()) as fhir:
            record = PatientRecord(fhir, patient_id="pt-1")
            for resource in ("Medication", "Practitioner", "Basic"):
                with pytest.raises(ValueError, match="cannot be searched by patient"):
                    await record.search_raw(resource)


def _smart_configuration(scopes: list[str]) -> dict[str, object]:
    return {
        "authorization_endpoint": "http://auth.example/authorize",
        "token_endpoint": "http://auth.example/token",
        "capabilities": ["permission-patient", "launch-standalone"],
        "scopes_supported": scopes,
        "code_challenge_methods_supported": ["S256"],
    }


async def test_without_a_wildcard_the_scope_lists_the_statement_types() -> None:
    supported = ["patient/Patient.read", "patient/Observation.read", "patient/Group.read"]
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200, json=_smart_configuration([*supported, "launch/patient"])
        )
        metadata = respx.get(f"{BASE}/metadata").respond(200, json=STATEMENT)
        smart = SmartClient(BASE, "app", "http://app.example/callback")
        request = await smart.authorization_request()
    assert metadata.called
    scope = httpx.URL(request.url).params["scope"]
    # Group is in scopes_supported but not in the statement; Condition the reverse.
    assert scope == "patient/Patient.read patient/Observation.read launch/patient"


async def test_with_a_wildcard_the_statement_is_not_fetched() -> None:
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200, json=_smart_configuration(["patient/*.read", "launch/patient"])
        )
        metadata = respx.get(f"{BASE}/metadata").respond(200, json=STATEMENT)
        smart = SmartClient(BASE, "app", "http://app.example/callback")
        request = await smart.authorization_request()
    assert not metadata.called
    assert httpx.URL(request.url).params["scope"] == "patient/*.read launch/patient"
