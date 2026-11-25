"""Regressions for the security findings S1-S6 in CODE-REVIEW.md."""

from __future__ import annotations

import logging
import stat
from pathlib import Path
from typing import Any

import pytest
import respx
from pytest import LogCaptureFixture

from aiofhir import FhirClient, PatientRecord
from aiofhir.log import (
    IsoFormatter,
    RedactingFilter,
    configure_logging,
    redact_params,
    redact_url,
)
from aiofhir.profiles.base import ServerProfile, medication_reference_id

HASTE = "https://api.haste.health/w/livekit/proj/api/v1/fhir"
BASE = "http://fhir.example/r4"


class _Tokens:
    async def get(self) -> str:
        return "access-secret"

    async def refresh(self, stale: str | None = None) -> str:
        return "access-secret"


def _bundle(resources: list[dict[str, object]], next_url: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "resourceType": "Bundle",
        "entry": [{"resource": resource} for resource in resources],
    }
    if next_url is not None:
        body["link"] = [{"relation": "next", "url": next_url}]
    return body


# S1: next links must stay under the FHIR base, not just the host.
@pytest.mark.parametrize(
    "next_url",
    [
        "https://api.haste.health/w/other/x/api/v1/fhir/Observation?page=2",
        f"{HASTE}/../../../../other/x/api/v1/fhir/Observation?page=2",
        "https://evil.example/w/livekit/proj/api/v1/fhir/Observation?page=2",
        "https://api.haste.health/w/livekit/proj/api/v1/fhir-evil/Observation",
    ],
)
async def test_next_link_outside_the_base_is_not_followed(next_url: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{HASTE}/Observation").respond(200, json=_bundle([], next_url))
        other = mock.route(host__regex=".*").respond(200, json=_bundle([]))
        async with FhirClient(HASTE, _Tokens()) as fhir:
            result = await fhir.search("Observation", {"category": "laboratory"})
    assert other.call_count == 0
    assert any("outside the FHIR base" in warning for warning in result.warnings)


async def test_next_link_under_the_base_is_followed() -> None:
    with respx.mock:
        route = respx.get(f"{HASTE}/Observation").mock(
            side_effect=[
                respx.MockResponse(200, json=_bundle([], f"{HASTE}/Observation?page=2")),
                respx.MockResponse(200, json=_bundle([])),
            ]
        )
        async with FhirClient(HASTE, _Tokens()) as fhir:
            await fhir.search("Observation")
    assert route.call_count == 2


# S2: ids and types are validated; references are parsed, not split.
@pytest.mark.parametrize("resource_id", ["../Account/1", "x?_format=xml", "a/b", "..", ""])
async def test_read_refuses_ids_that_change_the_path(resource_id: str) -> None:
    async with FhirClient(BASE, _Tokens()) as fhir:
        with pytest.raises(ValueError):
            await fhir.read("Medication", resource_id)


async def test_read_refuses_a_bad_resource_type() -> None:
    async with FhirClient(BASE, _Tokens()) as fhir:
        with pytest.raises(ValueError):
            await fhir.read("../Account", "1")


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("Medication/m1", "m1"),
        ("Medication/m1/_history/2", "m1"),
        ("https://other.example/fhir/Medication/x", None),
        ("Medication/x?_format=xml", None),
        ("Medication/../Account/1", None),
        ("Patient/p1", None),
    ],
)
def test_medication_reference_parsing(reference: str, expected: str | None) -> None:
    assert medication_reference_id(reference) == expected


async def test_foreign_medication_reference_is_not_read_and_display_is_used() -> None:
    calls: list[tuple[str, str]] = []

    async def read(resource_type: str, resource_id: str) -> dict[str, Any]:
        calls.append((resource_type, resource_id))
        return {}

    text = await ServerProfile().resolve_medication(
        {
            "medicationReference": {
                "reference": "https://other.example/fhir/Medication/x",
                "display": "Metformin",
            }
        },
        read,
    )
    assert calls == []
    assert text == "Metformin"


# S3 / S4: search_raw cannot widen the result or skip the forced patient.
@pytest.mark.parametrize(
    "params",
    [
        {"subject": "Patient/other"},
        {"subject:Patient": "other"},
        {"patient.identifier": "MRN|1"},
        {"patient:missing": "true"},
        {"_revinclude": "Provenance:target"},
        {"_include": "*"},
        {"_has:Observation:patient:code": "1234"},
        {"_id": "other"},
    ],
)
async def test_search_raw_refuses_widening_parameters(params: dict[str, str]) -> None:
    async with FhirClient(BASE, _Tokens()) as fhir:
        record = PatientRecord(fhir, patient_id="pt-1")
        with pytest.raises(ValueError):
            await record.search_raw("Observation", params)


async def test_search_raw_patient_uses_id_and_medication_is_not_searchable() -> None:
    with respx.mock:
        route = respx.get(f"{BASE}/Patient").respond(
            200, json=_bundle([{"resourceType": "Patient", "id": "pt-1"}])
        )
        async with FhirClient(BASE, _Tokens()) as fhir:
            record = PatientRecord(fhir, patient_id="pt-1")
            await record.search_raw("Patient")
            with pytest.raises(ValueError):
                await record.search_raw("Medication")
    params = route.calls.last.request.url.params
    assert params.get_list("_id") == ["pt-1"]
    assert "patient" not in params


async def test_search_raw_drops_resources_of_other_types() -> None:
    with respx.mock:
        respx.get(f"{BASE}/Observation").respond(
            200,
            json=_bundle(
                [
                    {"resourceType": "Observation", "id": "o1"},
                    {"resourceType": "Patient", "id": "someone-else"},
                ]
            ),
        )
        async with FhirClient(BASE, _Tokens()) as fhir:
            record = PatientRecord(fhir, patient_id="pt-1")
            result = await record.search_raw("Observation")
    assert [item["id"] for item in result.resources] == ["o1"]
    assert result.warnings


# S5: redaction covers modifiers, chains, ids in paths, and free text.
def test_redact_url_hides_person_ids_in_paths_and_modified_keys() -> None:
    url = (
        f"{BASE}/Patient/p123/_history/2?subject:Patient=p1&patient.identifier=MRN123"
        "&_id=p1&identifier=sys|mrn&name=Smith&category=laboratory"
    )
    redacted = redact_url(url)
    for secret in ("p123", "p1&", "MRN123", "mrn", "Smith"):
        assert secret not in redacted
    assert "/Patient/[id]/_history/" in redacted or "/Patient/%5Bid%5D" in redacted
    assert "category=laboratory" in redacted


def test_redact_params_matches_on_the_base_key() -> None:
    redacted = redact_params({"subject:Patient": ["p1"], "code": ["1234"]})
    assert redacted == {"subject:Patient": ["[redacted]"], "code": ["1234"]}


def test_redacting_filter_scrubs_tokens_and_person_references() -> None:
    record = logging.LogRecord(
        "aiofhir.seed",
        logging.INFO,
        __file__,
        1,
        "linking %s to %s with Bearer %s",
        ("User/u-1", "Patient/pt-9", "abc.def.ghi"),
        None,
    )
    RedactingFilter().filter(record)
    message = record.getMessage()
    assert "u-1" not in message
    assert "pt-9" not in message
    assert "abc.def.ghi" not in message
    assert "User/[id]" in message and "Patient/[id]" in message


async def test_read_debug_log_hides_the_patient_id_in_the_path(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="aiofhir.client")
    with respx.mock:
        respx.get(f"{BASE}/Patient/pt-secret").respond(200, json={"resourceType": "Patient"})
        async with FhirClient(BASE, _Tokens()) as fhir:
            await fhir.read("Patient", "pt-secret")
    assert "pt-secret" not in caplog.text


# Logging format: ISO 8601 with milliseconds and offset; library adds only NullHandler.
def test_iso_formatter_prints_a_timezone_aware_timestamp() -> None:
    record = logging.LogRecord("aiofhir.x", logging.INFO, __file__, 1, "hello", None, None)
    record.created = 1780000000.123
    line = IsoFormatter(utc=True).format(record)
    assert line.startswith("2026-05-28T20:26:40.123+00:00 INFO  aiofhir.x: hello")
    local = IsoFormatter().format(record)
    assert local[23] in "+-" and local[26] == ":"


def test_library_attaches_only_a_null_handler() -> None:
    import aiofhir

    handlers = logging.getLogger("aiofhir").handlers
    assert aiofhir.__version__
    assert handlers and all(isinstance(h, logging.NullHandler) for h in handlers)


def test_configure_logging_installs_one_redacting_iso_handler() -> None:
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    try:
        configure_logging("INFO")
        configure_logging("DEBUG")
        ours = [h for h in root.handlers if h.get_name() == "aiofhir-cli"]
        assert len(ours) == 1
        assert isinstance(ours[0].formatter, IsoFormatter)
        assert any(isinstance(f, RedactingFilter) for f in ours[0].filters)
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
        root.setLevel(saved_level)


# S6: env files that hold secrets are owner-only.
def test_save_env_value_makes_the_file_owner_only(tmp_path: Path) -> None:
    from haste.login import save_env_value

    env_file = tmp_path / ".env"
    env_file.write_text("BASE=x\n")
    env_file.chmod(0o644)
    save_env_value(env_file, "SMOKE_CLIENT_SECRET", "s3cret")
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_seed_login_scope_is_not_long_lived() -> None:
    from haste.login import SEED_LOGIN_SCOPE

    assert "offline_access" not in SEED_LOGIN_SCOPE.split()
