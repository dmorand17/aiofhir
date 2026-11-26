"""FhirError messages never carry ids or tokens; the raw OperationOutcome stays on .outcome."""

from __future__ import annotations

import pytest
import respx

from aiofhir import FhirClient
from aiofhir.errors import FhirError

BASE = "http://fhir.example/r4"


class _Tokens:
    async def get(self) -> str:
        return "access-secret"

    async def refresh(self, stale: str | None = None) -> str:
        return "access-secret"


def test_message_is_redacted() -> None:
    error = FhirError(404, "Patient/pt-secret not found (Bearer abc.def.ghi)")
    assert "pt-secret" not in str(error)
    assert "abc.def.ghi" not in str(error)
    assert "pt-secret" not in repr(error)
    assert "Patient/[id]" in str(error)
    assert error.status == 404
    assert error.outcome is None


async def test_operation_outcome_is_kept_raw_but_the_message_is_redacted() -> None:
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "not-found",
                "diagnostics": "Resource Patient/pt-secret not found",
            }
        ],
    }
    with respx.mock:
        respx.get(f"{BASE}/Observation/o1").respond(404, json=outcome)
        async with FhirClient(BASE, _Tokens()) as fhir:
            with pytest.raises(FhirError) as caught:
                await fhir.read("Observation", "o1")
    assert "pt-secret" not in str(caught.value)
    assert "not found" in str(caught.value)
    assert caught.value.status == 404
    assert caught.value.outcome == outcome


async def test_non_outcome_error_body_has_no_outcome() -> None:
    with respx.mock:
        respx.get(f"{BASE}/Observation/o1").respond(500, text="boom")
        async with FhirClient(BASE, _Tokens(), sleep=_no_sleep) as fhir:
            with pytest.raises(FhirError) as caught:
                await fhir.read("Observation", "o1")
    assert caught.value.outcome is None
    assert caught.value.status == 500


async def _no_sleep(_seconds: float) -> None:
    return None
