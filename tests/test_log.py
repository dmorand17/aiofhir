"""Log setup hides tokens and patient query values."""

from __future__ import annotations

import logging

import respx
from pytest import LogCaptureFixture

from aiofhir import FhirClient
from aiofhir.log import configure_logging, redact_params, redact_url

BASE = "http://fhir.example/r4"


def test_redact_url_hides_patient_query_values() -> None:
    url = "http://fhir.example/r4/Observation?patient=pt-secret&category=laboratory"
    redacted = redact_url(url)
    assert "pt-secret" not in redacted
    assert "patient=%5Bredacted%5D" in redacted or "patient=[redacted]" in redacted
    assert "category=laboratory" in redacted


def test_redact_params_hides_patient_and_keeps_other_values() -> None:
    redacted = redact_params({"patient": ["pt-secret"], "category": ["laboratory"]})
    assert redacted == {"patient": ["[redacted]"], "category": ["laboratory"]}


def test_configure_logging_accepts_debug_and_quiets_http_libraries() -> None:
    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    saved = (root.level, httpx_logger.level, httpcore_logger.level)
    try:
        configure_logging("DEBUG")
        assert root.level == logging.DEBUG
        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
    finally:
        root.setLevel(saved[0])
        httpx_logger.setLevel(saved[1])
        httpcore_logger.setLevel(saved[2])


def test_configure_logging_rejects_an_unknown_level() -> None:
    try:
        configure_logging("loud")
    except ValueError as exc:
        assert "loud" in str(exc)
    else:
        raise AssertionError("expected ValueError")


class _Tokens:
    async def get(self) -> str:
        return "access-secret"

    async def refresh(self, stale: str | None = None) -> str:
        return "access-secret"


async def test_client_debug_log_omits_the_token_and_patient_query(
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="aiofhir.client")
    with respx.mock:
        respx.get(f"{BASE}/Observation").respond(
            200, json={"resourceType": "Bundle", "type": "searchset", "entry": []}
        )
        async with FhirClient(BASE, _Tokens()) as fhir:
            await fhir.search("Observation", {"patient": "pt-secret", "category": "laboratory"})
    text = caplog.text
    assert "access-secret" not in text
    assert "Bearer" not in text
    assert "pt-secret" not in text
    assert "[redacted]" in text
    assert "status=200" in text
