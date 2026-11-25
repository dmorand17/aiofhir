"""Idempotent Haste seed decisions. The live server is not required."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from aiofhir.smart.discovery import SmartConfiguration
from haste.seeding import (
    CLIENT_ID,
    READ_POLICY_ID,
    SEED_PATIENT,
    SEED_SYSTEM,
    SeedUserError,
    apply_seed,
    build_appointments,
    build_chart,
    build_client_application,
    build_membership,
    build_patient,
    count_resources,
    haste_system_base,
)

WHEN = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
BASE = "http://h.example/fhir"


def _config() -> SmartConfiguration:
    return SmartConfiguration(
        authorization_endpoint="http://h.example/authorize",
        token_endpoint="http://h.example/token",
        capabilities=(
            "permission-patient",
            "permission-v2",
            "launch-standalone",
            "sso-openid-connect",
            "permission-offline",
        ),
        scopes_supported=(
            "patient/*.rs",
            "openid",
            "fhirUser",
            "launch/patient",
            "offline_access",
            "system/*.cruds",
        ),
        code_challenge_methods_supported=("S256",),
        token_endpoint_auth_methods_supported=("none",),
    )


def test_patient_uses_the_seed_identifier() -> None:
    patient = build_patient()
    identifier = patient["identifier"][0]
    assert patient["resourceType"] == "Patient"
    assert identifier["system"] == SEED_SYSTEM
    assert identifier["value"] == SEED_PATIENT
    assert patient["name"][0]["family"] == "Seed"


async def test_apply_seed_creates_a_patient_and_uses_it_for_appointments() -> None:
    with respx.mock:
        respx.get(f"{BASE}/Patient").respond(
            200, json={"resourceType": "Bundle", "total": 0, "entry": []}
        )
        created_patient = respx.post(f"{BASE}/Patient").respond(
            201, json={"resourceType": "Patient", "id": "new-pt"}
        )
        respx.get(f"{BASE}/ClientApplication/{CLIENT_ID}").respond(
            200, json={"resourceType": "ClientApplication", "id": CLIENT_ID}
        )
        respx.get(f"{BASE}/Appointment").respond(200, json={"resourceType": "Bundle", "total": 0})
        created_appointment = respx.post(f"{BASE}/Appointment").respond(201, json={})
        chart = respx.post(BASE).respond(200, json={"resourceType": "Bundle"})
        async with httpx.AsyncClient() as http:
            result = await apply_seed(
                http,
                BASE,
                "admin-token",
                now=WHEN,
                patient_id=None,
                configuration=_config(),
                secret="generated-secret",
            )
    assert result.patient_id == "new-pt"
    assert result.appointments_created == 6
    assert result.chart_loaded is True
    assert "Patient/new-pt" in chart.calls.last.request.content.decode()
    assert result.user_status == "skipped"
    assert result.membership_linked is False
    assert created_patient.called
    assert SEED_PATIENT in created_patient.calls.last.request.content.decode()
    appointment = created_appointment.calls.last.request.content.decode()
    assert "Patient/new-pt" in appointment
    assert "admin-token" not in appointment


async def test_apply_seed_reuses_the_seed_patient() -> None:
    with respx.mock:
        respx.get(f"{BASE}/Patient").respond(
            200,
            json={
                "resourceType": "Bundle",
                "total": 1,
                "entry": [{"resource": {"resourceType": "Patient", "id": "kept-pt"}}],
            },
        )
        created_patient = respx.post(f"{BASE}/Patient").respond(201, json={})
        respx.post(BASE).respond(200, json={"resourceType": "Bundle"})
        respx.get(f"{BASE}/ClientApplication/{CLIENT_ID}").respond(
            200, json={"resourceType": "ClientApplication", "id": CLIENT_ID}
        )
        respx.get(f"{BASE}/Appointment").respond(
            200, json={"resourceType": "Bundle", "entry": [{"resource": {"id": "appt-1"}}]}
        )
        respx.put(url__regex=rf"{BASE}/Appointment/.+").respond(200, json={})
        async with httpx.AsyncClient() as http:
            result = await apply_seed(
                http,
                BASE,
                "admin-token",
                now=WHEN,
                patient_id=None,
                configuration=_config(),
                secret="generated-secret",
            )
    assert result.patient_id == "kept-pt"
    assert result.appointments_created == 0
    assert not created_patient.called


def test_count_resources_ignores_other_types() -> None:
    bundle = {
        "entry": [
            {"resource": {"resourceType": "Practitioner", "id": "a"}},
            {"resource": {"resourceType": "PractitionerRole", "id": "b"}},
            {"resource": {"resourceType": "Practitioner", "id": "c"}},
        ]
    }
    assert count_resources(bundle, "Practitioner") == 2


def test_appointments_cover_booked_pending_and_cancelled() -> None:
    appointments = build_appointments("pt-1", WHEN)
    by_status: dict[str, list[str]] = {}
    for appointment in appointments:
        by_status.setdefault(str(appointment["status"]), []).append(str(appointment["start"]))
        identifier = appointment["identifier"][0]
        assert identifier["system"] == SEED_SYSTEM
        assert appointment["participant"][0]["actor"]["reference"] == "Patient/pt-1"
    assert by_status["booked"] == [
        "2026-06-04T12:00:00Z",
        "2026-06-15T12:00:00Z",
        "2026-07-16T12:00:00Z",
    ]
    assert by_status["pending"] == ["2026-06-08T12:00:00Z"]
    assert by_status["cancelled"] == ["2026-06-02T12:00:00Z"]
    assert by_status["fulfilled"] == ["2026-05-22T12:00:00Z"]


def test_chart_covers_inline_contained_and_referenced_medications() -> None:
    resources = build_chart("pt-1", WHEN)
    by_id = {resource["id"]: resource for resource in resources}
    assert by_id["mr-inline"]["medicationCodeableConcept"]["text"] == "Lisinopril"
    assert by_id["mr-contained"]["medicationReference"]["reference"] == "#med1"
    assert by_id["mr-remote"]["medicationReference"]["reference"] == "Medication/med-remote"
    assert by_id["med-remote"]["code"]["text"] == "Metformin"
    assert by_id["mr-stopped"]["status"] == "stopped"
    assert by_id["allergy-peanut"]["clinicalStatus"]["coding"][0]["code"] == "active"
    assert by_id["condition-asthma"]["category"][0]["coding"][0]["code"] == "problem-list-item"
    assert by_id["lab-a1c"]["category"][0]["coding"][0]["code"] == "laboratory"
    # Inside recent_labs' default 90-day window relative to the seed time.
    assert by_id["lab-a1c"]["effectiveDateTime"] == "2026-05-12T12:00:00Z"


def test_client_application_uses_the_derived_scope_and_code_response() -> None:
    application = build_client_application(_config(), secret="generated-secret")
    assert application["id"] == CLIENT_ID
    assert application["responseTypes"] == "code"
    assert application["grantType"] == ["authorization_code", "refresh_token"]
    assert application["redirectUri"] == ["http://localhost:8765/callback"]
    assert application["scope"] == "patient/*.rs launch/patient openid fhirUser offline_access"
    assert application["secret"] == "generated-secret"
    assert "system/*.cruds" not in application["scope"]


async def test_apply_seed_reloads_information_and_skips_existing_appointments() -> None:
    practitioners = {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [{"resource": {"resourceType": "Practitioner", "id": "pr-1"}}],
    }
    with respx.mock:
        batch = respx.post(BASE).respond(200, json={"resourceType": "Bundle"})
        respx.get(f"{BASE}/Practitioner").respond(200, json={"resourceType": "Bundle", "total": 1})
        respx.get(f"{BASE}/ClientApplication/{CLIENT_ID}").respond(
            200, json={"resourceType": "ClientApplication", "id": CLIENT_ID}
        )
        respx.get(f"{BASE}/Appointment").respond(
            200,
            json={"resourceType": "Bundle", "total": 1, "entry": [{"resource": {"id": "kept"}}]},
        )
        created = respx.post(f"{BASE}/Appointment").respond(201, json={})
        respx.put(url__regex=rf"{BASE}/Appointment/.+").respond(200, json={})
        async with httpx.AsyncClient() as http:
            result = await apply_seed(
                http,
                BASE,
                "admin-token",
                now=WHEN,
                patient_id="pt-1",
                configuration=_config(),
                secret="generated-secret",
                practitioner_bundle=practitioners,
                hospital_bundle={"resourceType": "Bundle", "type": "batch", "entry": []},
            )
    assert result.loaded_information is True
    assert result.created_secret is None
    assert result.appointments_created == 0
    assert batch.call_count == 3
    assert not created.called
    assert "admin-token" not in repr(result)


async def test_apply_seed_reloads_a_partial_information_index() -> None:
    practitioners = {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [
            {"resource": {"resourceType": "Practitioner", "id": "pr-1"}},
            {"resource": {"resourceType": "Practitioner", "id": "pr-2"}},
        ],
    }
    with respx.mock:
        batch = respx.post(BASE).respond(200, json={"resourceType": "Bundle"})
        respx.get(f"{BASE}/Practitioner").respond(200, json={"resourceType": "Bundle", "total": 1})
        respx.get(f"{BASE}/ClientApplication/{CLIENT_ID}").respond(
            200, json={"resourceType": "ClientApplication", "id": CLIENT_ID}
        )
        respx.get(f"{BASE}/Appointment").respond(
            200, json={"resourceType": "Bundle", "entry": [{"resource": {"id": "appt-1"}}]}
        )
        respx.put(url__regex=rf"{BASE}/Appointment/.+").respond(200, json={})
        async with httpx.AsyncClient() as http:
            result = await apply_seed(
                http,
                BASE,
                "admin-token",
                now=WHEN,
                patient_id="pt-1",
                configuration=_config(),
                secret="generated-secret",
                practitioner_bundle=practitioners,
                hospital_bundle={"resourceType": "Bundle", "type": "batch", "entry": []},
            )
    assert result.loaded_information is True
    assert batch.call_count == 3


async def test_apply_seed_loads_empty_information_and_creates_the_client() -> None:
    practitioners = {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [{"resource": {"resourceType": "Practitioner", "id": "pr-1"}}],
    }
    with respx.mock:
        batch = respx.post(BASE).respond(200, json={"resourceType": "Bundle"})
        respx.get(f"{BASE}/Practitioner").mock(
            side_effect=[
                httpx.Response(200, json={"resourceType": "Bundle", "total": 0}),
                httpx.Response(200, json={"resourceType": "Bundle", "total": 1}),
            ]
        )
        respx.get(f"{BASE}/ClientApplication/{CLIENT_ID}").respond(404)
        created = respx.put(f"{BASE}/ClientApplication/{CLIENT_ID}").respond(
            201, json={"resourceType": "ClientApplication", "id": CLIENT_ID}
        )
        respx.get(f"{BASE}/Appointment").respond(
            200, json={"resourceType": "Bundle", "entry": [{"resource": {"id": "appt-1"}}]}
        )
        respx.put(url__regex=rf"{BASE}/Appointment/.+").respond(200, json={})
        async with httpx.AsyncClient() as http:
            result = await apply_seed(
                http,
                BASE,
                "admin-token",
                now=WHEN,
                patient_id="pt-1",
                configuration=_config(),
                secret="generated-secret",
                practitioner_bundle=practitioners,
                hospital_bundle={"resourceType": "Bundle", "type": "batch", "entry": []},
            )
    assert result.loaded_information is True
    assert result.created_secret == "generated-secret"
    assert batch.call_count == 3
    body = created.calls.last.request.content.decode()
    assert "generated-secret" in body
    assert '"responseTypes":"code"' in body or '"responseTypes": "code"' in body
    assert "admin-token" not in body


def test_membership_links_the_user_to_the_patient() -> None:
    membership = build_membership("user-1", "pt-1")
    assert membership["user"]["reference"] == "User/user-1"
    assert membership["link"]["reference"] == "Patient/pt-1"


def test_system_base_is_the_tenant_system_project() -> None:
    assert (
        haste_system_base("https://api.haste.health/w/livekit/synthea-sandbox/api/v1/fhir/")
        == "https://api.haste.health/w/livekit/system/api/v1/fhir"
    )
    with pytest.raises(SeedUserError):
        haste_system_base("http://h.example/fhir")


HASTE = "https://api.haste.health/w/acme/demo/api/v1/fhir"
SYSTEM = "https://api.haste.health/w/acme/system/api/v1/fhir"


def _mock_project(mock: respx.MockRouter, membership: list[dict[str, object]]) -> dict[str, Any]:
    routes: dict[str, Any] = {}
    mock.get(f"{HASTE}/ClientApplication/{CLIENT_ID}").respond(
        200, json={"resourceType": "ClientApplication", "id": CLIENT_ID}
    )
    mock.get(f"{HASTE}/Appointment").respond(
        200, json={"resourceType": "Bundle", "entry": [{"resource": {"id": "appt-9"}}]}
    )
    routes["appointment_update"] = mock.put(url__regex=rf"{HASTE}/Appointment/.+").respond(
        200, json={}
    )
    routes["chart"] = mock.post(HASTE).respond(200, json={"resourceType": "Bundle"})
    routes["policy"] = mock.put(f"{HASTE}/AccessPolicyV2/{READ_POLICY_ID}").respond(
        200, json={"resourceType": "AccessPolicyV2", "id": READ_POLICY_ID}
    )
    routes["membership_search"] = mock.get(f"{HASTE}/Membership").respond(
        200, json={"resourceType": "Bundle", "entry": [{"resource": m} for m in membership]}
    )
    routes["membership_create"] = mock.post(f"{HASTE}/Membership").respond(
        201, json={"resourceType": "Membership", "id": "mem-new"}
    )
    routes["membership_update"] = mock.put(url__regex=rf"{HASTE}/Membership/.+").respond(
        200, json={"resourceType": "Membership"}
    )
    mock.get(f"{HASTE}/AccessPolicyV2Assignment").respond(
        200, json={"resourceType": "Bundle", "entry": []}
    )
    routes["assignment"] = mock.post(f"{HASTE}/AccessPolicyV2Assignment").respond(
        201, json={"resourceType": "AccessPolicyV2Assignment", "id": "asg-1"}
    )
    return routes


async def _seed(**kwargs: Any) -> Any:
    async with httpx.AsyncClient() as http:
        return await apply_seed(
            http,
            HASTE,
            "admin-token",
            now=WHEN,
            patient_id="pt-1",
            configuration=_config(),
            secret="generated-secret",
            **kwargs,
        )


async def test_seed_invites_the_patient_user_in_the_system_project() -> None:
    with respx.mock as mock:
        routes = _mock_project(mock, [])
        lookup = mock.get(f"{SYSTEM}/User").respond(
            200, json={"resourceType": "Bundle", "entry": []}
        )
        invite = mock.post(f"{SYSTEM}/User").respond(
            201, json={"resourceType": "User", "id": "user-9"}
        )
        result = await _seed(seed_user_email="doug+patient@example.com")
    assert lookup.calls.last.request.url.params["email"] == "doug+patient@example.com"
    body = json.loads(invite.calls.last.request.content)
    assert body == {"resourceType": "User", "email": "doug+patient@example.com", "role": "member"}
    assert result.user_status == "invited"
    assert result.user_id == "user-9"
    membership = json.loads(routes["membership_create"].calls.last.request.content)
    assert membership["user"]["reference"] == "User/user-9"
    assert membership["link"]["reference"] == "Patient/pt-1"
    assignment = json.loads(routes["assignment"].calls.last.request.content)
    assert assignment["link"]["reference"] == "Membership/mem-new"
    assert assignment["accessPolicy"]["reference"] == f"AccessPolicyV2/{READ_POLICY_ID}"
    policy = json.loads(routes["policy"].calls.last.request.content)
    assert "'Patient/pt-1' in %target.subject.reference" in json.dumps(policy)
    assert result.membership_linked and result.policy_assigned


async def test_seed_reuses_an_existing_user_and_uses_the_system_token() -> None:
    with respx.mock as mock:
        _mock_project(mock, [])
        lookup = mock.get(f"{SYSTEM}/User").respond(
            200,
            json={
                "resourceType": "Bundle",
                "entry": [{"resource": {"id": "user-1", "email": "p@example.com"}}],
            },
        )
        invite = mock.post(f"{SYSTEM}/User").respond(201, json={})
        result = await _seed(seed_user_email="p@example.com", system_token="system-token")
    assert lookup.calls.last.request.headers["authorization"] == "Bearer system-token"
    assert not invite.called
    assert result.user_status == "existing"
    assert result.user_id == "user-1"


async def test_seed_does_not_link_the_admin_without_a_patient_user() -> None:
    with respx.mock as mock:
        routes = _mock_project(mock, [])
        users = mock.route(url__startswith=SYSTEM).respond(500)
        result = await _seed()
    assert result.user_status == "skipped"
    assert not result.membership_linked
    assert not routes["membership_search"].called
    assert not routes["membership_create"].called
    assert not users.called


async def test_seed_with_a_user_id_relinks_an_existing_membership() -> None:
    existing: dict[str, object] = {
        "resourceType": "Membership",
        "id": "mem-1",
        "user": {"reference": "User/user-5"},
        "link": {"reference": "Patient/old"},
    }
    with respx.mock as mock:
        routes = _mock_project(mock, [existing])
        result = await _seed(seed_user_id="user-5")
    assert result.user_status == "given"
    assert routes["membership_search"].calls.last.request.url.params["user"] == "User/user-5"
    update = json.loads(routes["membership_update"].calls.last.request.content)
    assert update["id"] == "mem-1"
    assert update["link"]["reference"] == "Patient/pt-1"
    assert not routes["membership_create"].called
    assignment = json.loads(routes["assignment"].calls.last.request.content)
    assert assignment["link"]["reference"] == "Membership/mem-1"


async def test_seed_explains_a_forbidden_system_project() -> None:
    with respx.mock as mock:
        _mock_project(mock, [])
        mock.get(f"{SYSTEM}/User").respond(403)
        with pytest.raises(SeedUserError, match="SYSTEM_TOKEN"):
            await _seed(seed_user_email="p@example.com")


async def test_index_polling_backs_off_between_attempts() -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    practitioners = {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [{"resource": {"resourceType": "Practitioner", "id": "pr-1"}}],
    }
    with respx.mock as mock:
        _mock_project(mock, [])
        mock.get(f"{HASTE}/Practitioner").mock(
            side_effect=[
                httpx.Response(200, json={"resourceType": "Bundle", "total": 0}),
                httpx.Response(200, json={"resourceType": "Bundle", "total": 0}),
                httpx.Response(200, json={"resourceType": "Bundle", "total": 1}),
            ]
        )
        await _seed(practitioner_bundle=practitioners, sleep=sleep)
    assert slept == [0.5, 1.0]


async def test_reseed_moves_existing_appointments_to_fresh_dates() -> None:
    with respx.mock as mock:
        routes = _mock_project(mock, [])
        update = routes["appointment_update"]
        create = mock.post(f"{HASTE}/Appointment").respond(201, json={})
        result = await _seed()
    assert result.appointments_created == 0
    assert not create.called
    starts = {json.loads(call.request.content)["start"] for call in update.calls}
    assert starts == {a["start"] for a in build_appointments("pt-1", WHEN)}
    assert all(json.loads(call.request.content)["id"] == "appt-9" for call in update.calls)


async def test_a_failed_batch_entry_stops_the_seed() -> None:
    with respx.mock as mock:
        _mock_project(mock, [])
        mock.post(HASTE).respond(
            200,
            json={
                "resourceType": "Bundle",
                "type": "batch-response",
                "entry": [
                    {"response": {"status": "201 Created"}},
                    {"response": {"status": "400 Bad Request"}},
                ],
            },
        )
        with pytest.raises(RuntimeError, match="1 of 2 batch entries failed"):
            await _seed()


async def test_a_new_client_secret_is_handed_out_before_later_steps_fail() -> None:
    saved: list[str] = []
    with respx.mock as mock:
        _mock_project(mock, [])
        mock.get(f"{HASTE}/ClientApplication/{CLIENT_ID}").respond(404)
        mock.put(f"{HASTE}/ClientApplication/{CLIENT_ID}").respond(201, json={})
        mock.get(f"{SYSTEM}/User").respond(403)
        with pytest.raises(SeedUserError):
            await _seed(seed_user_email="p@example.com", on_client_secret=saved.append)
    assert saved == ["generated-secret"]


async def test_seed_refuses_to_relink_a_membership_that_is_not_a_patient() -> None:
    existing: dict[str, object] = {
        "resourceType": "Membership",
        "id": "mem-admin",
        "user": {"reference": "User/user-5"},
        "link": {"reference": "Practitioner/pr-1"},
    }
    with respx.mock as mock:
        routes = _mock_project(mock, [existing])
        with pytest.raises(RuntimeError, match="Practitioner"):
            await _seed(seed_user_id="user-5")
    assert not routes["membership_update"].called
    assert not routes["membership_create"].called


async def test_seed_invites_when_the_user_search_returns_someone_else() -> None:
    with respx.mock as mock:
        _mock_project(mock, [])
        mock.get(f"{SYSTEM}/User").respond(
            200,
            json={
                "resourceType": "Bundle",
                "entry": [{"resource": {"id": "admin", "email": "admin@example.com"}}],
            },
        )
        invite = mock.post(f"{SYSTEM}/User").respond(
            201, json={"resourceType": "User", "id": "user-9"}
        )
        result = await _seed(seed_user_email="P@example.com")
    assert invite.called
    assert result.user_id == "user-9"
