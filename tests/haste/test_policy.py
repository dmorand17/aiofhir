"""The seed patient's AccessPolicyV2 and the admin seed client lockdown (H1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from haste.policy_check import (
    APPROVED,
    DENIED,
    CaseResult,
    PolicyCase,
    client_is_confidential,
    describe_claims,
    evaluate_case,
    evaluate_parameters,
    gap_count,
    is_type_search,
    patient_token_problem,
    policy_cases,
    probe_cases,
    token_claims,
)
from haste.policy_model import PolicyError, decide, evaluate_policy, parse_request
from haste.seeding import (
    COMPARTMENT_TYPES,
    READ_POLICY_ID,
    SEED_CLIENT_ID,
    build_appointments,
    build_chart,
    build_read_policy,
    delete_seed_client,
    policy_allows_search,
    searchable_types,
    secure_seed_client,
)

PATIENT = "pt-1"
WHEN = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
BASE = "https://api.haste.health/w/acme/demo/api/v1/fhir"
TOKEN_URL = "https://api.haste.health/w/acme/demo/oidc/token"


def _store(patient_id: str = PATIENT) -> dict[str, dict[str, Any]]:
    """What the seed writes, plus one Observation of someone else."""
    store: dict[str, dict[str, Any]] = {
        f"Patient/{patient_id}": {"resourceType": "Patient", "id": patient_id},
        "Observation/obs-other": {
            "resourceType": "Observation",
            "id": "obs-other",
            "subject": {"reference": "Patient/someone-else"},
        },
    }
    for resource in build_chart(patient_id, WHEN):
        store[f"{resource['resourceType']}/{resource['id']}"] = resource
    for index, appointment in enumerate(build_appointments(patient_id, WHEN)):
        store[f"Appointment/appt-{index}"] = {**appointment, "id": f"appt-{index}"}
    return store


def _cases(*, allow_search: bool = False) -> list[PolicyCase]:
    return policy_cases(
        PATIENT,
        other_observation="obs-other",
        appointment_id="appt-1",
        allow_search=allow_search,
    )


MODES = pytest.mark.parametrize("allow_search", [False, True], ids=["no-search", "search"])


def _children(policy: dict[str, Any]) -> list[dict[str, Any]]:
    [top] = policy["rule"]
    return list(top["rule"])


# Policy shape


@MODES
def test_policy_is_one_any_rule_of_targeted_leaf_rules(allow_search: bool) -> None:
    policy = build_read_policy(PATIENT, allow_search=allow_search)
    assert policy["id"] == READ_POLICY_ID
    assert policy["engine"] == "rule-engine"
    [top] = policy["rule"]
    # Haste rejects a condition next to combineBehavior, and denies the whole policy as
    # soon as a top-level rule denies, so the alternatives sit under one "any" rule.
    assert top["combineBehavior"] == "any"
    assert "condition" not in top and "target" not in top
    for rule in top["rule"]:
        assert "rule" not in rule and "combineBehavior" not in rule
        assert rule.get("effect", "permit") == "permit"
        assert rule["condition"]["expression"]["language"] == "text/fhirpath"
        names = [child["name"] for child in top["rule"]]
        assert len(names) == len(set(names))
        if rule["name"] != "capabilities":
            target = rule["target"]["expression"]["expression"]
            assert target.startswith("%request.type = ")


@MODES
def test_policy_pins_the_patient_and_names_no_write_or_history(allow_search: bool) -> None:
    policy = build_read_policy(PATIENT, allow_search=allow_search)
    text = json.dumps(policy)
    assert f"Patient/{PATIENT}" in text
    for request_type in (
        "create", "update", "patch", "delete", "history", "vread",
        "batch", "transaction", "invoke", "compartment",
    ):  # fmt: skip
        assert f"'{request_type}'" not in text
    # Haste cannot see search parameters, and calls a system search type-level.
    assert "%request.parameters" not in text
    assert "%request.level" not in text
    assert "'system'" not in text


def test_searches_are_only_in_the_search_mode() -> None:
    assert not policy_allows_search(build_read_policy(PATIENT))
    with_search = build_read_policy(PATIENT, allow_search=True)
    assert policy_allows_search(with_search)
    [rule] = [r for r in _children(with_search) if r["name"] == "search-by-type"]
    assert rule["target"]["expression"]["expression"] == (
        "%request.type = 'search' and %request.resource_type.exists()"
    )
    for kind in searchable_types():
        assert f"'{kind}'" in rule["condition"]["expression"]["expression"]
    assert "Medication'" not in rule["condition"]["expression"]["expression"]
    assert not policy_allows_search({"rule": ["not a rule"]})


def test_policy_reads_the_target_for_compartment_reads() -> None:
    policy = build_read_policy(PATIENT)
    [attribute] = policy["attribute"]
    assert attribute["attributeId"] == "target"
    assert attribute["operation"]["type"] == "read"
    assert (
        attribute["operation"]["path"]["expression"] == "%request.resource_type + '/' + %request.id"
    )
    conditions = {
        rule["name"]: rule["condition"]["expression"]["expression"] for rule in _children(policy)
    }
    for element in COMPARTMENT_TYPES:
        name = f"read-by-{element.replace('.', '-')}"
        assert conditions[name] == f"'Patient/{PATIENT}' in %target.{element}.reference"


@pytest.mark.parametrize("bad", ["", "a b", "x' or true or '", "Patient/1", "a" * 65])
def test_policy_refuses_a_patient_id_that_is_not_a_fhir_id(bad: str) -> None:
    with pytest.raises(ValueError, match="FHIR id"):
        build_read_policy(bad)


# Policy behaviour, through the rule-engine model


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case.label)
def test_policy_grants_exactly_the_listed_cases(case: PolicyCase) -> None:
    policy = build_read_policy(PATIENT)
    assert not case.gap
    assert decide(policy, case.method, case.url, _store()) is case.allow


@pytest.mark.parametrize("case", _cases(allow_search=True), ids=lambda case: case.label)
def test_search_mode_grants_the_listed_cases_and_shows_its_gaps(case: PolicyCase) -> None:
    """With searches on, every type search of a searchable type is allowed: a gap."""
    policy = build_read_policy(PATIENT, allow_search=True)
    decided = decide(policy, case.method, case.url, _store())
    if case.gap:
        assert decided, "a gap the model denies means the gap list is wrong"
    else:
        assert decided is case.allow


def test_search_mode_gaps_are_the_parameter_only_denials() -> None:
    gaps = {case.label for case in _cases(allow_search=True) if case.gap}
    assert gaps == {
        "search every Patient",
        "search another Patient by _id",
        "search Patients by name",
        "search Observations without a patient",
        "search Observations by code only",
        "labs for another patient",
        "labs for two patients",
        "modified patient parameter",
        "negated own patient",
        "negated own Patient _id",
        "chained patient parameter",
        "subject instead of patient",
        "reverse chain on own Patient",
        "reverse chain on own labs",
    }
    assert not any(case.gap for case in _cases())
    assert is_type_search("Observation?patient=x")
    assert not is_type_search("Practitioner")
    assert not is_type_search("Patient/p/Observation")
    assert not is_type_search("?_type=Patient")


def test_the_policy_engine_cannot_see_search_parameters() -> None:
    """HasteHealth url.rs: ParsedParameters flattens to nothing, so every path is empty."""
    for expression, want in (
        ("%request.parameters.exists()", False),
        ("%request.parameters.patient.exists()", False),
        ("%request.parameters._id.empty()", True),
    ):
        policy = {
            "rule": [
                {
                    "name": "probe",
                    "condition": {
                        "expression": {"language": "text/fhirpath", "expression": expression}
                    },
                }
            ]
        }
        level = evaluate_policy(
            policy, parse_request("GET", f"Observation?patient={PATIENT}"), _store()
        )
        assert (level == 1) is want, expression


def test_the_reflection_matches_haste_for_system_search_history_and_operations() -> None:
    system = parse_request("GET", "?_type=Patient")
    assert (system.type, system.level, system.resource_type) == ("search", "type", None)
    for url in ("_history", "Observation/_history", f"Patient/{PATIENT}/_history"):
        history = parse_request("GET", url)
        assert (history.type, history.resource_type, history.id) == ("history", None, None)
    everything = parse_request("GET", f"Patient/{PATIENT}/$everything")
    assert (everything.type, everything.resource_type, everything.id) == ("invoke", None, None)
    compartment = parse_request("GET", f"Patient/{PATIENT}/Observation")
    assert (compartment.type, compartment.resource_type, compartment.id) == (
        "compartment",
        "Observation",
        None,
    )


def _previous_search_rules() -> list[dict[str, Any]]:
    """The search rules of the first H1 patch, which failed live on 2026-11-25."""
    p = PATIENT
    param = "%request.parameters.patient"
    kinds = (
        "('Observation' | 'Condition' | 'MedicationRequest' | 'AllergyIntolerance' | 'Appointment')"
    )

    def rule(name: str, target: str, condition: str) -> dict[str, Any]:
        return {
            "name": name,
            "target": {"expression": {"language": "text/fhirpath", "expression": target}},
            "condition": {"expression": {"language": "text/fhirpath", "expression": condition}},
        }

    return [
        rule(
            "search-patient",
            "%request.type = 'search' and %request.level = 'type'"
            " and %request.resource_type = 'Patient'",
            "%request.parameters._id.exists()"
            f" and %request.parameters._id.value.all($this = '{p}')",
        ),
        rule(
            "search-compartment",
            "%request.type = 'search' and %request.level = 'type'"
            f" and %request.resource_type in {kinds}",
            f"{param}.exists() and {param}.value.all($this = '{p}' or $this = 'Patient/{p}')",
        ),
    ]


def test_the_model_reproduces_the_live_failure_of_the_previous_rules() -> None:
    """Doug's live check: own searches denied, and ?_type=Patient a target error."""
    policy = build_read_policy(PATIENT)
    policy["rule"][0]["rule"] = [*policy["rule"][0]["rule"], *_previous_search_rules()]
    store = _store()
    for case in _cases(allow_search=True):
        if case.allow and is_type_search(case.url):
            assert not decide(policy, case.method, case.url, store), case.label
    with pytest.raises(PolicyError, match="target"):
        evaluate_policy(policy, parse_request("GET", "?_type=Patient"), store)


def test_a_target_check_that_ignores_the_patient_leaks_another_record() -> None:
    policy = build_read_policy(PATIENT)
    for rule in _children(policy):
        if rule["name"] == "read-by-subject":
            rule["condition"]["expression"]["expression"] = "%target.id.exists()"
    assert decide(policy, "GET", "Observation/obs-other", _store())


def test_another_patients_policy_denies_this_patients_record() -> None:
    policy = build_read_policy("pt-2")
    store = _store()
    for case in _cases():
        if case.read_only and case.url not in ("metadata", "Medication/med-remote"):
            assert not decide(policy, case.method, case.url, store), case.label


def test_a_compartment_read_of_a_missing_resource_is_denied() -> None:
    policy = build_read_policy(PATIENT)
    assert not decide(policy, "GET", "Observation/does-not-exist", _store())


@MODES
def test_no_case_errors_in_the_model(allow_search: bool) -> None:
    """Every request evaluates cleanly: a deny is a decision, not a parse accident."""
    policy = build_read_policy(PATIENT, allow_search=allow_search)
    for case in _cases(allow_search=allow_search):
        evaluate_policy(policy, parse_request(case.method, case.url), _store())


def test_the_old_read_anything_policy_fails_this_matrix() -> None:
    """The model catches H1: the pre-fix policy grants every read of every patient."""
    kinds = ("read", "vread", "search", "history", "compartment", "capabilities")
    condition = " or ".join(f"%request.type = '{kind}'" for kind in kinds)
    old = {
        "resourceType": "AccessPolicyV2",
        "engine": "rule-engine",
        "rule": [
            {
                "name": "read-only",
                "condition": {"expression": {"language": "text/fhirpath", "expression": condition}},
            }
        ],
    }
    store = _store()
    assert decide(old, "GET", "Patient", store)
    assert decide(old, "GET", "Observation/obs-other", store)
    assert decide(old, "GET", "_history", store)
    assert decide(old, "GET", "ClientApplication/aiofhir-smoke", store)


def test_the_model_fails_closed_like_haste() -> None:
    store = _store()
    with_condition = {
        "rule": [
            {
                "name": "bad",
                "combineBehavior": "any",
                "condition": {"expression": {"language": "text/fhirpath", "expression": "true"}},
                "rule": [],
            }
        ]
    }
    with pytest.raises(PolicyError):
        evaluate_policy(with_condition, parse_request("GET", "metadata"), store)
    not_boolean = {
        "rule": [
            {
                "name": "empty",
                "condition": {
                    "expression": {"language": "text/fhirpath", "expression": "%request.id = 'x'"}
                },
            }
        ]
    }
    assert not decide(not_boolean, "GET", "Patient", store)


# Admin seed client lockdown


async def test_a_public_seed_client_is_given_a_secret_and_loses_refresh() -> None:
    events: list[str] = []
    public = {
        "resourceType": "ClientApplication",
        "id": SEED_CLIENT_ID,
        "meta": {"versionId": "3"},
        "name": "aiofhir seed",
        "grantType": ["authorization_code", "refresh_token"],
        "responseTypes": "code",
        "redirectUri": ["http://localhost:8765/callback"],
        "scope": "user/*.cruds openid fhirUser",
    }
    with respx.mock as mock:
        mock.get(f"{BASE}/ClientApplication/{SEED_CLIENT_ID}").respond(200, json=public)

        def written(request: httpx.Request) -> httpx.Response:
            events.append("put")
            return httpx.Response(200, json={})

        put = mock.put(f"{BASE}/ClientApplication/{SEED_CLIENT_ID}").mock(side_effect=written)
        async with httpx.AsyncClient() as http:
            status = await secure_seed_client(
                http,
                BASE,
                "admin-token",
                new_secret="new-secret",
                on_secret=lambda secret: events.append(f"saved {secret}"),
            )
    assert status == "locked"
    assert events == ["saved new-secret", "put"]
    body = json.loads(put.calls.last.request.content)
    assert body["secret"] == "new-secret"
    assert body["grantType"] == ["authorization_code"]
    assert body["scope"] == public["scope"]
    assert body["redirectUri"] == public["redirectUri"]
    assert "meta" not in body


async def test_a_confidential_seed_client_is_left_alone() -> None:
    saved: list[str] = []
    with respx.mock as mock:
        mock.get(f"{BASE}/ClientApplication/{SEED_CLIENT_ID}").respond(
            200, json={"resourceType": "ClientApplication", "secret": "already"}
        )
        put = mock.put(f"{BASE}/ClientApplication/{SEED_CLIENT_ID}").respond(200)
        async with httpx.AsyncClient() as http:
            status = await secure_seed_client(
                http, BASE, "t", new_secret="unused", on_secret=saved.append
            )
    assert status == "confidential"
    assert not put.called
    assert saved == []


async def test_a_missing_seed_client_is_reported() -> None:
    with respx.mock as mock:
        mock.get(f"{BASE}/ClientApplication/{SEED_CLIENT_ID}").respond(404)
        async with httpx.AsyncClient() as http:
            status = await secure_seed_client(
                http, BASE, "t", new_secret="unused", on_secret=lambda _: None
            )
    assert status == "absent"


async def test_a_failed_lock_keeps_the_saved_secret_and_says_so() -> None:
    saved: list[str] = []
    with respx.mock as mock:
        mock.get(f"{BASE}/ClientApplication/{SEED_CLIENT_ID}").respond(
            200, json={"resourceType": "ClientApplication", "id": SEED_CLIENT_ID}
        )
        mock.put(f"{BASE}/ClientApplication/{SEED_CLIENT_ID}").respond(403)
        async with httpx.AsyncClient() as http:
            with pytest.raises(RuntimeError, match="may still be public"):
                await secure_seed_client(http, BASE, "t", new_secret="s", on_secret=saved.append)
    assert saved == ["s"]


@pytest.mark.parametrize(("status", "expected"), [(204, True), (200, True), (404, False)])
async def test_delete_seed_client(status: int, expected: bool) -> None:
    with respx.mock as mock:
        route = mock.delete(f"{BASE}/ClientApplication/{SEED_CLIENT_ID}").respond(status)
        async with httpx.AsyncClient() as http:
            assert await delete_seed_client(http, BASE, "t") is expected
    assert route.calls.last.request.headers["authorization"] == "Bearer t"


async def test_delete_seed_client_raises_on_errors() -> None:
    with respx.mock as mock:
        mock.delete(f"{BASE}/ClientApplication/{SEED_CLIENT_ID}").respond(500)
        async with httpx.AsyncClient() as http:
            with pytest.raises(RuntimeError, match="delete failed"):
                await delete_seed_client(http, BASE, "t")


def test_the_seed_only_locks_its_own_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from haste import seed

    monkeypatch.delenv("CLIENT_ID", raising=False)
    assert seed._seed_client_id() == SEED_CLIENT_ID
    monkeypatch.setenv("CLIENT_ID", SEED_CLIENT_ID)
    assert seed._seed_client_id() == SEED_CLIENT_ID
    monkeypatch.setenv("CLIENT_ID", "doug-console-client")
    assert seed._seed_client_id() is None


# Live check helpers


@pytest.mark.parametrize(
    ("status", "error", "confidential"),
    [(401, "invalid_client", True), (400, "invalid_grant", False)],
)
async def test_client_confidentiality_probe(status: int, error: str, confidential: bool) -> None:
    with respx.mock as mock:
        route = mock.post(TOKEN_URL).respond(status, json={"error": error})
        async with httpx.AsyncClient() as http:
            assert await client_is_confidential(http, TOKEN_URL, SEED_CLIENT_ID) is confidential
    sent = route.calls.last.request.content.decode()
    assert f"client_id={SEED_CLIENT_ID}" in sent
    assert "client_secret" not in sent
    assert "authorization" not in route.calls.last.request.headers


async def test_client_confidentiality_probe_refuses_to_guess() -> None:
    with respx.mock as mock:
        mock.post(TOKEN_URL).respond(400, json={"error": "invalid_request"})
        async with httpx.AsyncClient() as http:
            with pytest.raises(RuntimeError, match="unexpected token response"):
                await client_is_confidential(http, TOKEN_URL, SEED_CLIENT_ID)


def test_evaluate_parameters_name_the_user_and_one_request() -> None:
    case = PolicyCase("x", "POST", "Observation", False, {"resourceType": "Observation"})
    body = evaluate_parameters("user-9", case)
    user, request = body["parameter"]
    assert user == {"name": "user", "valueReference": {"reference": "User/user-9"}}
    [entry] = request["resource"]["entry"]
    assert entry["request"] == {"method": "POST", "url": "Observation"}
    assert entry["resource"] == {"resourceType": "Observation"}


@pytest.mark.parametrize(
    ("status", "diagnostics", "outcome"),
    [(200, APPROVED, "allow"), (200, DENIED, "deny"), (400, "bad request", "error")],
)
async def test_evaluate_case_reads_the_outcome(status: int, diagnostics: str, outcome: str) -> None:
    case = PolicyCase("read", "GET", "Patient/pt-1", True)
    outcome_body = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "information", "code": "informational", "diagnostics": diagnostics}],
    }
    with respx.mock as mock:
        route = mock.post(f"{BASE}/AccessPolicyV2/{READ_POLICY_ID}/$evaluate-policy").respond(
            status, json=outcome_body
        )
        async with httpx.AsyncClient() as http:
            result = await evaluate_case(http, BASE, "admin", user_id="u", case=case)
    assert result.outcome == outcome
    assert route.calls.last.request.headers["authorization"] == "Bearer admin"


def test_a_deny_case_passes_on_deny_or_error_and_an_allow_case_only_on_allow() -> None:
    allow = PolicyCase("a", "GET", "Patient/p", True)
    deny = PolicyCase("d", "GET", "Patient", False)
    assert CaseResult(allow, "allow").passed
    assert not CaseResult(allow, "error").passed
    assert not CaseResult(allow, "deny").passed
    assert CaseResult(deny, "deny").passed
    assert CaseResult(deny, "error").passed
    assert not CaseResult(deny, "allow").passed
    assert not CaseResult(deny, "leak").passed
    gap = PolicyCase("g", "GET", "Observation", False, gap=True)
    assert CaseResult(gap, "allow").passed
    assert CaseResult(gap, "deny").passed
    assert not CaseResult(gap, "leak").passed
    assert not CaseResult(gap, "error").passed
    assert gap_count([CaseResult(gap, "allow"), CaseResult(gap, "deny")]) == 1


def test_policy_cases_never_write_for_real() -> None:
    cases = _cases()
    writes = [case for case in cases if not case.read_only]
    assert writes and all(not case.allow for case in writes)
    assert {case.url for case in cases} >= {"_history", "?_type=Patient", "Patient"}


async def test_probe_cases_skips_writes_and_flags_foreign_data() -> None:
    cases = [
        PolicyCase("own", "GET", f"Observation?patient={PATIENT}", True),
        PolicyCase("all", "GET", "Patient", False),
        PolicyCase("leaky", "GET", "Observation", False),
        PolicyCase("write", "DELETE", "Observation/lab-a1c", False),
    ]

    def bundle(resource_id: str, patient: str) -> dict[str, Any]:
        observation = {
            "resourceType": "Observation",
            "id": resource_id,
            "subject": {"reference": f"Patient/{patient}"},
        }
        return {"resourceType": "Bundle", "entry": [{"resource": observation}]}

    own, leak = bundle("o1", PATIENT), bundle("o2", "other")
    with respx.mock as mock:
        mock.get(f"{BASE}/Observation", params={"patient": PATIENT}).respond(200, json=own)
        mock.get(f"{BASE}/Patient").respond(403)
        mock.get(f"{BASE}/Observation").respond(200, json=leak)
        deleted = mock.delete(url__startswith=BASE).respond(204)
        async with httpx.AsyncClient() as http:
            results = await probe_cases(http, BASE, "user", patient_id=PATIENT, cases=cases)
    assert not deleted.called
    assert [(r.case.label, r.outcome, r.passed) for r in results] == [
        ("own", "allow", True),
        ("all", "deny", True),
        ("leaky", "leak", False),
    ]


# Whose token is it?


def _jwt(claims: dict[str, Any]) -> str:
    import base64

    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.signature"


def _patient_claims(**overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "sub": "user-9",
        "https://haste.health/user_id": "user-9",
        "https://haste.health/user_role": "member",
        "fhirUser": f"Patient/{PATIENT}",
        "https://haste.health/access_policies": ["v1"],
        "client_id": SEED_CLIENT_ID,
    }
    claims.update(overrides)
    return claims


def test_token_claims_decode_without_the_signature() -> None:
    claims = _patient_claims()
    assert token_claims(_jwt(claims)) == claims
    assert token_claims("opaque-token") == {}
    assert token_claims("a.!!!.c") == {}


def test_the_patient_member_token_passes() -> None:
    claims = token_claims(_jwt(_patient_claims()))
    assert patient_token_problem(claims, user_id="user-9", patient_id=PATIENT) is None
    text = describe_claims(claims)
    assert "user=user-9" in text and "role=member" in text and "access_policies=1" in text


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"sub": "doug", "https://haste.health/user_id": "doug", "fhirUser": None},
            "reused another Haste session",
        ),
        ({"https://haste.health/user_role": "owner"}, "applies access policies only"),
        ({"https://haste.health/user_role": "admin"}, "applies access policies only"),
        ({"fhirUser": "Patient/someone-else"}, "not Patient/"),
        ({"https://haste.health/access_policies": []}, "no access policy"),
    ],
)
def test_a_token_that_skips_or_lacks_the_policy_is_refused(
    overrides: dict[str, Any], reason: str
) -> None:
    claims = _patient_claims(**overrides)
    problem = patient_token_problem(claims, user_id="user-9", patient_id=PATIENT)
    assert problem is not None and reason in problem
    assert patient_token_problem({}, user_id="user-9", patient_id=PATIENT)


def test_describe_claims_never_includes_the_token() -> None:
    token = _jwt(_patient_claims())
    assert token not in describe_claims(token_claims(token))
