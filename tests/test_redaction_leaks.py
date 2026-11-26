"""A table of shapes that must not leak through redaction, and values that must survive it."""

from __future__ import annotations

import pytest

from aiofhir.errors import FhirError
from aiofhir.log import redact_params, redact_text, redact_url
from aiofhir.scrub import scrub

BASE = "https://fhir.example/r4"

TEXT_LEAKS = [
    ("lowercase bearer", "authorization: bearer tok.SECRET.x", "tok.SECRET.x"),
    ("upper bearer", "AUTHORIZATION: BEARER SECRETtok", "SECRETtok"),
    ("bearer in quotes", '{"Authorization": "Bearer SECRETtok"}', "SECRETtok"),
    ("basic header", "Authorization: Basic Y2lkOlNFQ1JFVA==", "Y2lkOlNFQ1JFVA=="),
    ("client_secret json", '{"client_secret": "SECRETcs"}', "SECRETcs"),
    ("code_verifier json", '{"code_verifier":"SECRETcv"}', "SECRETcv"),
    ("client_assertion json", '{"client_assertion": "eyJSECRET"}', "eyJSECRET"),
    ("access_token dict repr", "{'access_token': 'SECRETat', 'x': 1}", "SECRETat"),
    ("refresh_token dict repr", "{'refresh_token': 'SECRETrt'}", "SECRETrt"),
    ("form body", "grant_type=refresh_token&refresh_token=SECRETrt&x=1", "SECRETrt"),
    ("query string", "/cb?access_token=SECRETat&state=s", "SECRETat"),
    ("form client_secret", "client_id=a&client_secret=SECRETcs", "SECRETcs"),
    ("person ref", "linking Patient/pt-SECRET", "pt-SECRET"),
    ("encoded person ref", "GET /Appointment?actor=Patient%2Fpt-SECRET", "pt-SECRET"),
    ("absolute person ref", "see https://x/fhir/Patient/pt-SECRET", "pt-SECRET"),
    ("bare patient param", "search failed for patient=pt-SECRET", "pt-SECRET"),
    ("modified param", "subject:Patient=pt-SECRET&code=1", "pt-SECRET"),
    ("actor param", "actor=pt-SECRET", "pt-SECRET"),
    ("content search", "_content=Jane&code=1", "Jane"),
]


@pytest.mark.parametrize(("case", "text", "secret"), TEXT_LEAKS, ids=[c[0] for c in TEXT_LEAKS])
def test_redact_text_hides(case: str, text: str, secret: str) -> None:
    del case
    assert secret not in redact_text(text)


@pytest.mark.parametrize(
    "text",
    [
        '{"client_secret": "SECRETcs"}',
        "{'access_token': 'SECRETat'}",
        "refresh_token=SECRETrt",
        "Bearer SECRETtok",
        "bearer SECRETtok",
    ],
)
def test_scrub_hides_credentials(text: str) -> None:
    assert "SECRET" not in scrub(text)


URL_LEAKS = [
    ("actor ref", f"{BASE}/Appointment?actor=Patient/pt-SECRET&date=ge2026", "pt-SECRET"),
    ("performer ref", f"{BASE}/Observation?performer=Patient/pt-SECRET", "pt-SECRET"),
    ("requester", f"{BASE}/MedicationRequest?requester=Practitioner/dr-SECRET", "dr-SECRET"),
    ("participant", f"{BASE}/Encounter?participant=dr-SECRET", "dr-SECRET"),
    ("general-practitioner", f"{BASE}/Patient?general-practitioner=dr-SECRET", "dr-SECRET"),
    ("_text", f"{BASE}/Condition?_text=Jane+SECRET", "SECRET"),
    ("_content", f"{BASE}/DocumentReference?_content=Jane+SECRET", "SECRET"),
    ("ref under any key", f"{BASE}/Observation?focus=Patient/pt-SECRET", "pt-SECRET"),
    ("encoded ref under any key", f"{BASE}/Observation?focus=Patient%2Fpt-SECRET", "pt-SECRET"),
    ("absolute ref", f"{BASE}/Observation?focus={BASE}/Patient/pt-SECRET", "pt-SECRET"),
    ("token in query", f"{BASE}/cb?access_token=SECRETat", "SECRETat"),
]


@pytest.mark.parametrize(("case", "url", "secret"), URL_LEAKS, ids=[c[0] for c in URL_LEAKS])
def test_redact_url_hides(case: str, url: str, secret: str) -> None:
    del case
    assert secret not in redact_url(url)


def test_redact_params_hides_person_refs_under_any_key() -> None:
    redacted = redact_params(
        {"actor": ["Patient/p1"], "focus": ["Patient/p2"], "_text": ["Jane"], "code": ["1234"]}
    )
    assert redacted == {
        "actor": ["[redacted]"],
        "focus": ["Patient/[id]"],
        "_text": ["[redacted]"],
        "code": ["1234"],
    }


def test_fhir_error_message_hides_query_style_ids() -> None:
    error = FhirError(400, "invalid search patient=pt-SECRET&actor=Patient%2Fpt-SECRET")
    assert "pt-SECRET" not in str(error)


KEPT = [
    "category=laboratory",
    "code=http://loinc.org|4548-4",
    "Basic metabolic panel",
    "Observation/obs-1",
    "Practitioner/dr-1",
    "resource_id=abc",
    '{"code": "4548-4"}',
    "status=200",
]


@pytest.mark.parametrize("text", KEPT)
def test_redact_text_keeps_clinical_and_non_person_values(text: str) -> None:
    assert redact_text(text) == text


def test_redaction_is_idempotent() -> None:
    text = "Patient/pt-1 patient=pt-1 Bearer abc {'access_token': 'x'}"
    once = redact_text(text)
    assert redact_text(once) == once
