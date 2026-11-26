"""Checks that the seed patient's AccessPolicyV2 reaches only that patient's record.

Three checks, each usable from ``python -m haste.check_policy`` and the opt-in
live tests:

* ``client_is_confidential``: a token request for the admin seed client
  without a secret. Haste authenticates the client before it looks at the
  code, so a confidential client answers ``invalid_client`` and a public one
  ``invalid_grant``. No user is involved and nothing is created.
* ``evaluate_cases``: runs each request in ``policy_cases`` through Haste's
  ``AccessPolicyV2/{id}/$evaluate-policy`` as the patient User. That only
  evaluates the policy, it never runs the request, so the write cases are safe.
  It needs an admin or owner token, which may name another user.
* ``probe_cases``: sends the read-only cases for real with a token the patient
  user got with ``user/*.rs``, the widest token they could ask for.

Haste's policy engine cannot see search parameters (see
``haste.seeding.build_read_policy``), so the expected results depend on how the
policy was seeded. By default searches are denied. With ``SEED_ALLOW_SEARCH=1``
they are allowed by type, and the searches the policy cannot tell apart from
the patient's own (another patient's id, no patient, modifiers, chains) are
*gaps*: Haste allows them, and only a ``patient/`` scope keeps the results to
the patient. A gap passes ``$evaluate-policy`` either way and is reported; a
gap that returns another patient's data to a real token fails.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from haste.seeding import (
    CLIENT_ID,
    READ_POLICY_ID,
    REDIRECT_URI,
    SEED_SYSTEM,
    policy_allows_search,
    raise_if_token_rejected,
    searchable_types,
)

# An id no seed writes. The policy decides on the id alone, so it need not exist.
OTHER_PATIENT = "aiofhir-not-the-seed-patient"
APPROVED = "Policy approved user access."
DENIED = "Policy denied user access."
Json = dict[str, Any]


@dataclass(frozen=True)
class PolicyCase:
    label: str
    method: str
    url: str
    allow: bool
    body: Json | None = None
    # Allowed by the type-level search rule although it is not the patient's own
    # search: the policy cannot see parameters. Only the token's scope confines it.
    gap: bool = False

    @property
    def read_only(self) -> bool:
        return self.method == "GET"


@dataclass(frozen=True)
class CaseResult:
    case: PolicyCase
    outcome: str
    detail: str = ""

    @property
    def passed(self) -> bool:
        if self.case.gap:
            return self.outcome in ("allow", "deny")
        if self.case.allow:
            return self.outcome == "allow"
        # A request Haste cannot even parse is not granted either. A broken
        # request format would also fail every allow case, so it cannot pass
        # the run unnoticed.
        return self.outcome in ("deny", "error")


def policy_cases(
    patient_id: str,
    *,
    other_patient: str = OTHER_PATIENT,
    other_observation: str | None = None,
    appointment_id: str | None = None,
    allow_search: bool = False,
) -> list[PolicyCase]:
    """What the seed patient may and may not do, as relative FHIR request URLs.

    ``allow_search`` matches the seed's ``SEED_ALLOW_SEARCH``: without it the
    patient's own searches are expected to be denied too; with it, the deny
    cases that are type searches of a searchable type become gaps.
    """
    p, o = patient_id, other_patient
    reads = [
        ("capabilities", "metadata"),
        ("read own Patient", f"Patient/{p}"),
        ("read own Observation", "Observation/lab-a1c"),
        ("read own Condition", "Condition/condition-asthma"),
        ("read own AllergyIntolerance", "AllergyIntolerance/allergy-peanut"),
        ("read own MedicationRequest", "MedicationRequest/mr-inline"),
        ("read own Immunization", "Immunization/imm-flu"),
        ("read own Encounter", "Encounter/enc-checkup"),
        ("read own Procedure", "Procedure/proc-spirometry"),
        ("read own DocumentReference", "DocumentReference/doc-visit-note"),
        ("read a referenced Medication", "Medication/med-remote"),
    ]
    if appointment_id:
        reads.append(("read own Appointment", f"Appointment/{appointment_id}"))
    searches = [
        ("search own Patient by _id", f"Patient?_id={p}"),
        ("labs for own patient", f"Observation?patient={p}&category=laboratory"),
        ("patient as a reference", f"Observation?patient=Patient/{p}"),
        ("appointments", f"Appointment?patient={p}&_sort=date&_count=1&_offset=1"),
        ("medication requests", f"MedicationRequest?patient={p}&status=active"),
        ("allergies", f"AllergyIntolerance?patient={p}&clinical-status=active"),
        ("conditions", f"Condition?patient={p}&category=problem-list-item"),
        ("immunizations", f"Immunization?patient={p}&status=completed"),
        ("vital signs", f"Observation?patient={p}&category=vital-signs"),
        ("past visits", f"Encounter?patient={p}&status=finished"),
        ("procedures", f"Procedure?patient={p}&status=completed"),
        ("documents", f"DocumentReference?patient={p}&status=current"),
    ]
    deny = [
        ("read another Patient", f"Patient/{o}"),
        ("search every Patient", "Patient"),
        ("search another Patient by _id", f"Patient?_id={o}"),
        ("search Patients by name", "Patient?name=Seed"),
        ("search Observations without a patient", "Observation"),
        ("search Observations by code only", "Observation?code=4548-4"),
        ("labs for another patient", f"Observation?patient={o}"),
        ("labs for two patients", f"Observation?patient={p},{o}"),
        ("modified patient parameter", "Observation?patient:missing=false"),
        ("negated own patient", f"Observation?patient:not={p}"),
        ("negated own Patient _id", f"Patient?_id:not={p}"),
        ("chained patient parameter", "Observation?patient.name=Seed"),
        ("subject instead of patient", f"Observation?subject=Patient/{o}"),
        ("reverse chain on own Patient", f"Patient?_id={p}&_has:Observation:patient:code=x"),
        ("reverse chain on own labs", f"Observation?patient={p}&_has:Provenance:target:agent=x"),
        ("search Practitioners", "Practitioner"),
        ("search Medications", "Medication"),
        ("read the smoke client", f"ClientApplication/{CLIENT_ID}"),
        ("search clients", "ClientApplication"),
        ("search Memberships", "Membership"),
        ("read the access policy", f"AccessPolicyV2/{READ_POLICY_ID}"),
        ("system history", "_history"),
        ("type history", "Observation/_history"),
        ("own Patient history", f"Patient/{p}/_history"),
        ("system search", "?_type=Patient"),
        ("compartment search", f"Patient/{p}/Observation"),
        ("$everything", f"Patient/{p}/$everything"),
    ]
    if other_observation:
        deny.append(("read another patient's Observation", f"Observation/{other_observation}"))
    cases = [PolicyCase(label, "GET", url, True) for label, url in reads]
    cases += [PolicyCase(label, "GET", url, allow_search) for label, url in searches]
    cases += [
        PolicyCase(label, "GET", url, False, gap=allow_search and is_type_search(url))
        for label, url in deny
    ]
    cases += [
        PolicyCase(
            "create an Observation",
            "POST",
            "Observation",
            False,
            {
                "resourceType": "Observation",
                "status": "final",
                "code": {"text": "aiofhir policy check"},
                "subject": {"reference": f"Patient/{p}"},
                "identifier": [{"system": SEED_SYSTEM, "value": "policy-check"}],
            },
        ),
        PolicyCase("delete own Observation", "DELETE", "Observation/lab-a1c", False),
    ]
    return cases


ROLE_CLAIM = "https://haste.health/user_role"
USER_ID_CLAIM = "https://haste.health/user_id"
PATIENT_CLAIM = "https://haste.health/patient"
POLICIES_CLAIM = "https://haste.health/access_policies"


def token_claims(access_token: str) -> Json:
    """The JWT payload of a Haste access token, unverified. Empty when it is not a JWT.

    Used only to check whose token it is. The token itself is never printed.
    """
    parts = access_token.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(payload)
    except (binascii.Error, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def describe_claims(claims: Mapping[str, Any]) -> str:
    """Who a token is for, without the token: user, role, fhirUser, patient, client."""
    policies = claims.get(POLICIES_CLAIM)
    fields = [
        f"user={claims.get(USER_ID_CLAIM) or claims.get('sub')}",
        f"role={claims.get(ROLE_CLAIM)}",
        f"fhirUser={claims.get('fhirUser')}",
        f"patient={claims.get(PATIENT_CLAIM)}",
        f"client={claims.get('client_id')}",
        f"access_policies={len(policies) if isinstance(policies, list) else 0}",
    ]
    return " ".join(fields)


def patient_token_problem(
    claims: Mapping[str, Any], *, user_id: str, patient_id: str
) -> str | None:
    """Why a token is not the seed patient user's member token, or None when it is.

    Haste skips access policies for an ``admin`` or ``owner`` role
    (``access_control.rs``), so probing with such a token proves nothing.
    """
    if not claims:
        return "the access token is not a JWT, so whose it is cannot be checked"
    subject = claims.get(USER_ID_CLAIM) or claims.get("sub")
    if subject != user_id:
        return (
            f"the token belongs to user {subject}, not the patient user {user_id}: the browser "
            "most likely reused another Haste session"
        )
    role = claims.get(ROLE_CLAIM)
    if role != "member":
        return (
            f"the patient user has role {role!r}; Haste applies access policies only to "
            "'member'. Change the user's role to member in the Haste admin console"
        )
    fhir_user = claims.get("fhirUser")
    if fhir_user is not None and fhir_user != f"Patient/{patient_id}":
        return f"the token's fhirUser is {fhir_user}, not Patient/{patient_id}"
    policies = claims.get(POLICIES_CLAIM)
    if not isinstance(policies, list) or not policies:
        return "the token carries no access policy; run just seed, then sign in again"
    return None


def is_type_search(url: str) -> bool:
    """Whether a relative URL is a type-level search of a searchable type."""
    path = url.split("?", 1)[0]
    return "/" not in path and path in searchable_types()


async def fetch_policy_allows_search(
    http: httpx.AsyncClient, base: str, admin_token: str
) -> bool | None:
    """Whether the stored seed policy allows searches; None when it is missing."""
    response = await http.get(
        f"{base.rstrip('/')}/AccessPolicyV2/{READ_POLICY_ID}", headers=_headers(admin_token)
    )
    raise_if_token_rejected(response, "read the access policy")
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise RuntimeError(f"access policy read failed ({response.status_code})")
    body = response.json()
    return policy_allows_search(body) if isinstance(body, dict) else None


def gap_count(results: list[CaseResult]) -> int:
    """Gap cases Haste allowed: searches only a patient/ scope confines."""
    return sum(1 for result in results if result.case.gap and result.outcome == "allow")


def evaluate_parameters(user_id: str, case: PolicyCase) -> Json:
    """The $evaluate-policy input: the user, and a one-entry Bundle with the request."""
    entry: Json = {"request": {"method": case.method, "url": case.url}}
    if case.body is not None:
        entry["resource"] = case.body
    return {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "user", "valueReference": {"reference": f"User/{user_id}"}},
            {
                "name": "request",
                "resource": {"resourceType": "Bundle", "type": "batch", "entry": [entry]},
            },
        ],
    }


async def evaluate_case(
    http: httpx.AsyncClient,
    base: str,
    admin_token: str,
    *,
    user_id: str,
    case: PolicyCase,
) -> CaseResult:
    response = await http.post(
        f"{base.rstrip('/')}/AccessPolicyV2/{READ_POLICY_ID}/$evaluate-policy",
        json=evaluate_parameters(user_id, case),
        headers=_headers(admin_token),
    )
    raise_if_token_rejected(response, "evaluate the policy")
    text = response.text
    if response.status_code < 400 and APPROVED in text:
        return CaseResult(case, "allow")
    if response.status_code < 400 and DENIED in text:
        return CaseResult(case, "deny")
    return CaseResult(case, "error", f"HTTP {response.status_code}: {_diagnostics(text)}")


async def evaluate_cases(
    http: httpx.AsyncClient,
    base: str,
    admin_token: str,
    *,
    user_id: str,
    cases: list[PolicyCase],
) -> list[CaseResult]:
    return [
        await evaluate_case(http, base, admin_token, user_id=user_id, case=case) for case in cases
    ]


async def probe_cases(
    http: httpx.AsyncClient,
    base: str,
    user_token: str,
    *,
    patient_id: str,
    cases: list[PolicyCase],
) -> list[CaseResult]:
    """Send the GET cases with the patient user's token. Writes are never sent.

    A granted case must answer 2xx with only the patient's own data. A denied
    case passes when it is refused (401 or 403), or when it answers with
    nothing that belongs to anyone else.
    """
    results: list[CaseResult] = []
    root = base.rstrip("/")
    for case in cases:
        if not case.read_only:
            continue
        url = f"{root}{case.url}" if case.url.startswith("?") else f"{root}/{case.url}"
        response = await http.get(url, headers=_headers(user_token))
        status = response.status_code
        if status in (401, 403):
            results.append(CaseResult(case, "deny", f"HTTP {status}"))
            continue
        if status >= 400:
            results.append(CaseResult(case, "error", f"HTTP {status}"))
            continue
        foreign = _foreign(_resources(response), patient_id)
        if foreign:
            results.append(CaseResult(case, "leak", f"returned {', '.join(foreign[:5])}"))
        elif case.allow:
            results.append(CaseResult(case, "allow", f"HTTP {status}"))
        else:
            results.append(CaseResult(case, "deny", f"HTTP {status} with only own data"))
    return results


async def client_is_confidential(
    http: httpx.AsyncClient,
    token_endpoint: str,
    client_id: str,
) -> bool:
    """Whether the token endpoint refuses ``client_id`` without its secret."""
    response = await http.post(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": "aiofhir-policy-check",
            "code_verifier": "aiofhir-policy-check-" + "0" * 32,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
        },
        headers={"Accept": "application/json"},
    )
    error = _oauth_error(response)
    if error == "invalid_client":
        return True
    if error == "invalid_grant":
        return False
    raise RuntimeError(f"unexpected token response ({response.status_code}, {error or 'no error'})")


async def find_other_observation(
    http: httpx.AsyncClient, base: str, admin_token: str, patient_id: str
) -> str | None:
    """An Observation of some other patient, for a read the policy must deny."""
    response = await http.get(
        f"{base.rstrip('/')}/Observation",
        params={"_count": "20"},
        headers=_headers(admin_token),
    )
    raise_if_token_rejected(response, "search Observation")
    if response.status_code >= 400:
        return None
    for resource in _resources(response):
        if resource.get("resourceType") != "Observation" or not resource.get("id"):
            continue
        names_a_patient = any("Patient/" in ref for ref in _references(resource))
        if names_a_patient and _foreign([resource], patient_id):
            return str(resource["id"])
    return None


async def find_appointment(
    http: httpx.AsyncClient, base: str, admin_token: str, patient_id: str
) -> str | None:
    response = await http.get(
        f"{base.rstrip('/')}/Appointment",
        params={"patient": patient_id, "_count": "1"},
        headers=_headers(admin_token),
    )
    raise_if_token_rejected(response, "search Appointment")
    if response.status_code >= 400:
        return None
    for resource in _resources(response):
        if resource.get("id"):
            return str(resource["id"])
    return None


def format_results(results: list[CaseResult]) -> str:
    lines = []
    for result in results:
        mark = "ok  " if result.passed else "FAIL"
        if result.passed and result.case.gap and result.outcome == "allow":
            mark = "GAP "
        want = "allow" if result.case.allow else "deny"
        detail = f"  ({result.detail})" if result.detail else ""
        lines.append(
            f"{mark} want {want:5} got {result.outcome:5} {result.case.method} {result.case.url}"
            f"  [{result.case.label}]{detail}"
        )
    return "\n".join(lines)


def _resources(response: httpx.Response) -> list[Json]:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return []
    if not isinstance(body, dict):
        return []
    if body.get("resourceType") != "Bundle":
        return [body]
    found: list[Json] = []
    for entry in body.get("entry") or []:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if isinstance(resource, dict):
            found.append(resource)
    return found


_SHARED = ("Medication", "CapabilityStatement", "OperationOutcome")


def _references(resource: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for key in ("subject", "patient"):
        value = resource.get(key)
        if isinstance(value, dict) and isinstance(value.get("reference"), str):
            found.append(value["reference"])
    for participant in resource.get("participant") or []:
        actor = participant.get("actor") if isinstance(participant, dict) else None
        if isinstance(actor, dict) and isinstance(actor.get("reference"), str):
            found.append(actor["reference"])
    return found


def _foreign(resources: list[Json], patient_id: str) -> list[str]:
    """``Type/id`` of every resource that is not the patient's own or shared data."""
    own = f"Patient/{patient_id}"
    foreign: list[str] = []
    for resource in resources:
        kind = str(resource.get("resourceType") or "")
        if kind in _SHARED:
            continue
        label = f"{kind}/{resource.get('id')}"
        if kind == "Patient":
            if resource.get("id") != patient_id:
                foreign.append(label)
            continue
        refs = [ref for ref in _references(resource) if "Patient/" in ref]
        if not refs or not any(ref == own or ref.endswith(f"/{own}") for ref in refs):
            foreign.append(label)
    return foreign


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/fhir+json",
        "Content-Type": "application/fhir+json",
    }


def _oauth_error(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return str(error) if error else None


def _diagnostics(text: str) -> str:
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return text[:200]
    issues = body.get("issue") if isinstance(body, dict) else None
    for issue in issues or []:
        if isinstance(issue, dict) and issue.get("diagnostics"):
            return str(issue["diagnostics"])[:200]
    return text[:200]
