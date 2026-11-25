"""Idempotent resources for a Haste smoke project.

Information files are posted on every run. Their entries use ifNoneExist,
so an existing practitioner index does not skip the load.

Haste keeps identity at the tenant level: a ``User`` lives in the tenant's
``system`` project (``/w/{tenant}/system/api/v1/fhir/User``), while the
``Membership`` that links that user to a ``Patient``, the ``AccessPolicyV2``
and its ``AccessPolicyV2Assignment`` live in the regular project. A User that
an admin creates for someone else is an invitation: the person must verify the
email address before they can sign in.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from aiofhir.smart.discovery import SmartConfiguration
from aiofhir.smart.scopes import scopes_from_capabilities

SEED_SYSTEM = "https://aiofhir.dev/seed"
SEED_PATIENT = "seed-patient"
CLIENT_ID = "aiofhir-smoke"
REDIRECT_URI = "http://localhost:8765/callback"
READ_POLICY_ID = "aiofhir-seed-patient-read"
Json = dict[str, Any]
Sleep = Callable[[float], Awaitable[None]]
_HASTE_BASE = re.compile(r"^(https?://[^/]+/w/[^/]+)/[^/]+(/api/v1/fhir)/?$")
_READ_TYPES = ("read", "vread", "search", "history", "compartment", "capabilities")


class SeedUserError(RuntimeError):
    """The seed could not find or invite the patient User."""


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeedResult:
    loaded_information: bool
    created_secret: str | None
    appointments_created: int
    patient_id: str | None = None
    membership_linked: bool = False
    user_id: str | None = None
    user_status: str = "skipped"
    chart_loaded: bool = False
    policy_assigned: bool = False


def haste_system_base(base: str) -> str:
    """The tenant's system-project FHIR base for a Haste project base."""
    match = _HASTE_BASE.match(base.rstrip("/"))
    if match is None:
        raise SeedUserError(
            "BASE is not a Haste project base (/w/{tenant}/{project}/api/v1/fhir); set SYSTEM_BASE"
        )
    return f"{match.group(1)}/system{match.group(2)}"


def build_user(email: str) -> Json:
    """A tenant member. Haste treats it as an invitation to that address."""
    return {"resourceType": "User", "email": email, "role": "member"}


def build_read_policy() -> Json:
    """Read and search only. Patient scopes bind the token to one compartment."""
    condition = " or ".join(f"%request.type = '{kind}'" for kind in _READ_TYPES)
    return {
        "resourceType": "AccessPolicyV2",
        "id": READ_POLICY_ID,
        "name": "aiofhir seed patient read-only",
        "description": "Read-only access for the aiofhir smoke patient.",
        "engine": "rule-engine",
        "rule": [
            {
                "name": "read-only",
                "combineBehavior": "any",
                "condition": {"expression": {"language": "text/fhirpath", "expression": condition}},
            }
        ],
    }


def build_policy_assignment(membership_id: str) -> Json:
    return {
        "resourceType": "AccessPolicyV2Assignment",
        "accessPolicy": {"reference": f"AccessPolicyV2/{READ_POLICY_ID}"},
        "link": {"reference": f"Membership/{membership_id}"},
    }


def build_patient() -> Json:
    """The smoke patient. A later run finds it by the seed identifier."""
    return {
        "resourceType": "Patient",
        "identifier": _identifier(SEED_PATIENT),
        "name": [{"family": "Seed", "given": ["Aiofhir"]}],
        "gender": "unknown",
    }


def build_membership(user_id: str, patient_id: str) -> Json:
    """Link the signed-in user to the seed patient so a standalone token has a patient."""
    return {
        "resourceType": "Membership",
        "user": {"reference": f"User/{user_id}"},
        "link": {"reference": f"Patient/{patient_id}"},
    }


def count_resources(bundle: Mapping[str, Any], resource_type: str) -> int:
    count = 0
    for entry in bundle.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        if isinstance(resource, dict) and resource.get("resourceType") == resource_type:
            count += 1
    return count


def build_appointments(patient_id: str, now: datetime) -> list[Json]:
    """A past visit, booked visits at +3, +14 and +45 days, one pending and one cancelled."""
    specs = (
        ("appt-past", "fulfilled", -10),
        ("appt-booked-3", "booked", 3),
        ("appt-booked-14", "booked", 14),
        ("appt-booked-45", "booked", 45),
        ("appt-pending", "pending", 7),
        ("appt-cancelled", "cancelled", 1),
    )
    return [
        _appointment(patient_id, value, status, now + timedelta(days=days))
        for value, status, days in specs
    ]


def build_chart(patient_id: str, now: datetime) -> list[Json]:
    """A small chart covering the medication shapes the helper resolves."""
    subject = {"reference": f"Patient/{patient_id}"}
    return [
        _request(
            "mr-inline",
            patient_id,
            status="active",
            medication={"medicationCodeableConcept": {"text": "Lisinopril"}},
        ),
        _request(
            "mr-contained",
            patient_id,
            status="active",
            medication={
                "contained": [
                    {"resourceType": "Medication", "id": "med1", "code": {"text": "Aspirin"}}
                ],
                "medicationReference": {"reference": "#med1"},
            },
        ),
        {
            "resourceType": "Medication",
            "id": "med-remote",
            "identifier": _identifier("med-remote"),
            "code": {"text": "Metformin"},
        },
        _request(
            "mr-remote",
            patient_id,
            status="active",
            medication={"medicationReference": {"reference": "Medication/med-remote"}},
        ),
        _request(
            "mr-stopped",
            patient_id,
            status="stopped",
            medication={"medicationCodeableConcept": {"text": "Stopped drug"}},
        ),
        {
            "resourceType": "AllergyIntolerance",
            "id": "allergy-peanut",
            "identifier": _identifier("allergy-peanut"),
            "patient": subject,
            "clinicalStatus": _status("active"),
            "code": {"text": "Peanut"},
        },
        {
            "resourceType": "Condition",
            "id": "condition-asthma",
            "identifier": _identifier("condition-asthma"),
            "subject": subject,
            "clinicalStatus": _status("active"),
            "category": [{"coding": [{"code": "problem-list-item"}]}],
            "code": {"text": "Asthma"},
        },
        {
            "resourceType": "Observation",
            "id": "lab-a1c",
            "identifier": _identifier("lab-a1c"),
            "subject": subject,
            "status": "final",
            "category": [{"coding": [{"code": "laboratory"}]}],
            "code": {"text": "A1c"},
            "effectiveDateTime": (now - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "valueQuantity": {"value": 6.1, "unit": "%"},
        },
    ]


def build_chart_bundle(patient_id: str, now: datetime) -> Json:
    """PUT each chart resource by id, so reruns update rather than duplicate."""
    return {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [
            {
                "resource": resource,
                "request": {"method": "PUT", "url": f"{resource['resourceType']}/{resource['id']}"},
            }
            for resource in build_chart(patient_id, now)
        ],
    }


def build_client_application(configuration: SmartConfiguration, *, secret: str | None) -> Json:
    """A public or confidential client allowed to request the derived scope."""
    application: Json = {
        "resourceType": "ClientApplication",
        "id": CLIENT_ID,
        "name": "aiofhir smoke",
        "grantType": ["authorization_code", "refresh_token"],
        "responseTypes": "code",
        "redirectUri": [REDIRECT_URI],
        "scope": scopes_from_capabilities(configuration),
    }
    if secret:
        application["secret"] = secret
    return application


async def apply_seed(
    http: httpx.AsyncClient,
    base: str,
    token: str,
    *,
    now: datetime,
    patient_id: str | None,
    configuration: SmartConfiguration,
    secret: str,
    practitioner_bundle: Mapping[str, Any] | None = None,
    hospital_bundle: Mapping[str, Any] | None = None,
    seed_user_email: str | None = None,
    seed_user_id: str | None = None,
    system_base: str | None = None,
    system_token: str | None = None,
    sleep: Sleep = asyncio.sleep,
) -> SeedResult:
    """Seed the project, then link a patient User when one is named.

    The patient User is ``seed_user_id`` when given, else the tenant User with
    ``seed_user_email`` (invited when missing). With neither, no Membership is
    written: the seed no longer links the signed-in admin to the seed patient.
    """
    root = base.rstrip("/")
    loaded = False
    if practitioner_bundle is not None:
        loaded = await _ensure_information(
            http, root, token, practitioner_bundle, hospital_bundle, sleep
        )
    created_secret = await _ensure_client(http, root, token, configuration, secret)
    used_patient = patient_id or await _ensure_patient(http, root, token)
    created_appointments = await _ensure_appointments(http, root, token, used_patient, now)
    await _post_bundle(http, root, token, build_chart_bundle(used_patient, now))
    user_id, user_status = await _resolve_user(
        http,
        root,
        token,
        seed_user_email=seed_user_email,
        seed_user_id=seed_user_id,
        system_base=system_base,
        system_token=system_token,
    )
    linked = False
    assigned = False
    if user_id is not None:
        await _put(http, f"{root}/AccessPolicyV2/{READ_POLICY_ID}", token, build_read_policy())
        membership_id = await _ensure_membership(http, root, token, user_id, used_patient)
        linked = True
        assigned = await _ensure_assignment(http, root, token, membership_id)
    return SeedResult(
        loaded_information=loaded,
        created_secret=created_secret,
        appointments_created=created_appointments,
        patient_id=used_patient,
        membership_linked=linked,
        user_id=user_id,
        user_status=user_status,
        chart_loaded=True,
        policy_assigned=assigned,
    )


async def _resolve_user(
    http: httpx.AsyncClient,
    base: str,
    token: str,
    *,
    seed_user_email: str | None,
    seed_user_id: str | None,
    system_base: str | None,
    system_token: str | None,
) -> tuple[str | None, str]:
    if seed_user_id:
        return seed_user_id, "given"
    if not seed_user_email:
        logger.info("no SEED_USER_EMAIL or SEED_USER_ID; skipping the patient user")
        return None, "skipped"
    system = (system_base or haste_system_base(base)).rstrip("/")
    bearer = system_token or token
    response = await http.get(
        f"{system}/User", params={"email": seed_user_email}, headers=_headers(bearer)
    )
    _raise_for_user(response, "look up")
    found = _first_entry(response)
    if found and found.get("id"):
        logger.info("reusing the patient user")
        return str(found["id"]), "existing"
    logger.info("inviting the patient user")
    created = await http.post(
        f"{system}/User", json=build_user(seed_user_email), headers=_headers(bearer)
    )
    _raise_for_user(created, "create")
    body = created.json()
    created_id = body.get("id") if isinstance(body, dict) else None
    if not created_id:
        raise SeedUserError("User create did not return an id")
    return str(created_id), "invited"


def _raise_for_user(response: httpx.Response, action: str) -> None:
    if response.status_code in (401, 403):
        raise SeedUserError(
            f"could not {action} the User in the system project ({response.status_code}). "
            "Users live at /w/{tenant}/system/api/v1/fhir and need a tenant admin token "
            "for that project: set SYSTEM_TOKEN, or invite the user in the Haste admin "
            "console and set SEED_USER_ID."
        )
    if response.status_code >= 400:
        raise SeedUserError(f"User {action} failed ({response.status_code})")


async def _ensure_information(
    http: httpx.AsyncClient,
    base: str,
    token: str,
    practitioner_bundle: Mapping[str, Any],
    hospital_bundle: Mapping[str, Any] | None,
    sleep: Sleep = asyncio.sleep,
) -> bool:
    if hospital_bundle is not None:
        await _post_bundle(http, base, token, hospital_bundle)
    await _post_bundle(http, base, token, practitioner_bundle)
    queries = _practitioner_queries(practitioner_bundle)
    for attempt in range(10):
        if attempt:
            await sleep(min(0.5 * attempt, 3.0))
        indexed = True
        for query in queries:
            if await _total(http, base, token, "Practitioner", query) < 1:
                indexed = False
                break
        if indexed:
            return True
    raise RuntimeError("practitioners were not indexed")


def _practitioner_queries(bundle: Mapping[str, Any]) -> list[dict[str, str]]:
    """Search params that find each Practitioner after an idempotent post."""
    queries: list[dict[str, str]] = []
    for entry in bundle.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        if not isinstance(resource, dict) or resource.get("resourceType") != "Practitioner":
            continue
        identifiers = resource.get("identifier") or []
        if identifiers and isinstance(identifiers[0], dict):
            system = identifiers[0].get("system")
            value = identifiers[0].get("value")
            if system and value:
                queries.append({"identifier": f"{system}|{value}"})
                continue
        resource_id = resource.get("id")
        if resource_id:
            queries.append({"_id": str(resource_id)})
    return queries


async def _ensure_patient(http: httpx.AsyncClient, base: str, token: str) -> str:
    identifier = f"{SEED_SYSTEM}|{SEED_PATIENT}"
    found = await _first(http, base, token, "Patient", {"identifier": identifier})
    if found and found.get("id"):
        logger.info("reusing seed patient")
        return str(found["id"])
    logger.info("creating seed patient %s", SEED_PATIENT)
    response = await http.post(
        f"{base}/Patient",
        json=build_patient(),
        headers=_headers(token),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"patient create failed ({response.status_code})")
    body = response.json()
    created_id = body.get("id") if isinstance(body, dict) else None
    if not created_id:
        raise RuntimeError("patient create did not return an id")
    logger.debug("created seed patient %s", created_id)
    return str(created_id)


async def _ensure_membership(
    http: httpx.AsyncClient,
    base: str,
    token: str,
    user_id: str,
    patient_id: str,
) -> str:
    """Link the patient User to the seed patient. Returns the Membership id."""
    found = await _first(http, base, token, "Membership", {"user": f"User/{user_id}"})
    if found and found.get("id") and _membership_patient(found) == patient_id:
        logger.info("reusing the membership")
        return str(found["id"])
    membership = build_membership(user_id, patient_id)
    if found and found.get("id"):
        logger.info("linking the membership to the seed patient")
        response = await http.put(
            f"{base}/Membership/{found['id']}",
            json={**membership, "id": found["id"]},
            headers=_headers(token),
        )
    else:
        logger.info("creating a membership")
        response = await http.post(
            f"{base}/Membership",
            json=membership,
            headers=_headers(token),
        )
    if response.status_code >= 400:
        raise RuntimeError(f"membership write failed ({response.status_code})")
    if found and found.get("id"):
        return str(found["id"])
    body = response.json()
    created_id = body.get("id") if isinstance(body, dict) else None
    if not created_id:
        raise RuntimeError("membership create did not return an id")
    return str(created_id)


async def _ensure_assignment(
    http: httpx.AsyncClient,
    base: str,
    token: str,
    membership_id: str,
) -> bool:
    found = await _first(
        http,
        base,
        token,
        "AccessPolicyV2Assignment",
        {
            "link": f"Membership/{membership_id}",
            "access-policy": f"AccessPolicyV2/{READ_POLICY_ID}",
        },
    )
    if found:
        return True
    response = await http.post(
        f"{base}/AccessPolicyV2Assignment",
        json=build_policy_assignment(membership_id),
        headers=_headers(token),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"policy assignment failed ({response.status_code})")
    return True


async def _put(http: httpx.AsyncClient, url: str, token: str, resource: Json) -> None:
    response = await http.put(url, json=resource, headers=_headers(token))
    if response.status_code >= 400:
        raise RuntimeError(f"{resource['resourceType']} write failed ({response.status_code})")


def _first_entry(response: httpx.Response) -> Json | None:
    body = response.json()
    entries = body.get("entry") if isinstance(body, dict) else None
    if not entries or not isinstance(entries[0], dict):
        return None
    resource = entries[0].get("resource")
    return resource if isinstance(resource, dict) else None


def _membership_patient(resource: Json) -> str | None:
    link = resource.get("link")
    if not isinstance(link, dict):
        return None
    reference = link.get("reference")
    if not isinstance(reference, str) or not reference.startswith("Patient/"):
        return None
    return reference.removeprefix("Patient/")


async def _ensure_client(
    http: httpx.AsyncClient,
    base: str,
    token: str,
    configuration: SmartConfiguration,
    secret: str,
) -> str | None:
    response = await http.get(f"{base}/ClientApplication/{CLIENT_ID}", headers=_headers(token))
    if response.status_code == 200:
        return None
    if response.status_code != 404:
        raise RuntimeError(f"client lookup failed ({response.status_code})")
    application = build_client_application(configuration, secret=secret)
    created = await http.put(
        f"{base}/ClientApplication/{CLIENT_ID}",
        json=application,
        headers=_headers(token),
    )
    if created.status_code >= 400:
        raise RuntimeError(f"client create failed ({created.status_code})")
    return secret


async def _ensure_appointments(
    http: httpx.AsyncClient,
    base: str,
    token: str,
    patient_id: str,
    now: datetime,
) -> int:
    created = 0
    for appointment in build_appointments(patient_id, now):
        value = str(appointment["identifier"][0]["value"])
        found = await _total(
            http,
            base,
            token,
            "Appointment",
            {"patient": patient_id, "identifier": f"{SEED_SYSTEM}|{value}"},
        )
        if found:
            continue
        response = await http.post(
            f"{base}/Appointment",
            json=appointment,
            headers=_headers(token),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"appointment create failed ({response.status_code})")
        created += 1
    return created


async def _first(
    http: httpx.AsyncClient,
    base: str,
    token: str,
    resource_type: str,
    params: Mapping[str, str],
) -> Json | None:
    response = await http.get(
        f"{base}/{resource_type}",
        params={"_count": "1", **dict(params)},
        headers=_headers(token),
    )
    logger.debug("GET %s status=%s", resource_type, response.status_code)
    if response.status_code >= 400:
        raise RuntimeError(f"{resource_type} search failed ({response.status_code})")
    return _first_entry(response)


async def _total(
    http: httpx.AsyncClient,
    base: str,
    token: str,
    resource_type: str,
    params: Mapping[str, str] | None = None,
) -> int:
    query = {"_count": "0", **dict(params or {})}
    response = await http.get(
        f"{base}/{resource_type}",
        params=query,
        headers=_headers(token),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"{resource_type} search failed ({response.status_code})")
    body = response.json()
    total = body.get("total") if isinstance(body, dict) else None
    return total if isinstance(total, int) else 0


async def _post_bundle(
    http: httpx.AsyncClient,
    base: str,
    token: str,
    bundle: Mapping[str, Any],
) -> None:
    logger.info("posting bundle (%s entries)", len(bundle.get("entry") or []))
    response = await http.post(base, json=dict(bundle), headers=_headers(token))
    logger.debug("POST bundle status=%s", response.status_code)
    if response.status_code >= 400:
        raise RuntimeError(f"bundle post failed ({response.status_code})")


def _appointment(patient_id: str, value: str, status: str, start: datetime) -> Json:
    return {
        "resourceType": "Appointment",
        "status": status,
        "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "identifier": _identifier(value),
        "participant": [{"actor": {"reference": f"Patient/{patient_id}"}, "status": "accepted"}],
        "description": value,
    }


def _request(resource_id: str, patient_id: str, *, status: str, medication: Json) -> Json:
    return {
        "resourceType": "MedicationRequest",
        "id": resource_id,
        "identifier": _identifier(resource_id),
        "status": status,
        "intent": "order",
        "subject": {"reference": f"Patient/{patient_id}"},
        **medication,
    }


def _identifier(value: str) -> list[Json]:
    return [{"system": SEED_SYSTEM, "value": value}]


def _status(code: str) -> Json:
    return {"coding": [{"code": code}]}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/fhir+json",
        "Content-Type": "application/fhir+json",
    }
