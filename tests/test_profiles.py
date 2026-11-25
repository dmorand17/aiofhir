"""Profile search quirks. Helpers stay free of vendor branches."""

from __future__ import annotations

from datetime import UTC, datetime

import respx

from aiofhir import FhirClient, PatientRecord
from aiofhir.profiles import CernerProfile, EpicProfile, GenericProfile, HasteProfile

_GE = "ge2026-06-01T12:00:00Z"
_LT = "lt2026-08-30T12:00:00Z"
_OPEN_LT = "lt9999-12-31T00:00:00Z"


def test_generic_adapt_search_passes_params_through() -> None:
    params = {"date": [_GE, _LT], "_sort": ["date"]}
    assert GenericProfile().adapt_search("Appointment", params) == {
        "date": [_GE, _LT],
        "_sort": ["date"],
    }


def test_haste_adapt_search_keeps_sort_and_both_date_bounds() -> None:
    params = {"date": [_GE, _LT], "_sort": ["date"]}
    assert HasteProfile().adapt_search("Appointment", params) == {
        "date": [_GE, _LT],
        "_sort": ["date"],
    }


def test_epic_adapt_search_drops_sort() -> None:
    adapted = EpicProfile().adapt_search(
        "Appointment",
        {"patient": ["pt-1"], "date": [_GE, _LT], "_sort": ["date"]},
    )
    assert "_sort" not in adapted
    assert adapted["patient"] == ["pt-1"]
    assert adapted["date"] == [_GE, _LT]


def test_epic_adapt_search_drops_sort_for_observations() -> None:
    adapted = EpicProfile().adapt_search(
        "Observation",
        {"category": ["laboratory"], "_sort": ["date"]},
    )
    assert adapted == {"category": ["laboratory"]}


def test_cerner_adapt_search_adds_lt_bound_when_only_ge_is_present() -> None:
    adapted = CernerProfile().adapt_search(
        "Appointment",
        {"patient": ["pt-1"], "date": [_GE], "_sort": ["date"]},
    )
    assert adapted["date"] == [_GE, _OPEN_LT]
    assert adapted["_sort"] == ["date"]


def test_cerner_adapt_search_keeps_an_existing_lt_bound() -> None:
    adapted = CernerProfile().adapt_search("Appointment", {"date": [_GE, _LT]})
    assert adapted["date"] == [_GE, _LT]


def test_cerner_does_not_add_lt_to_other_resources() -> None:
    adapted = CernerProfile().adapt_search("Observation", {"date": [_GE]})
    assert adapted["date"] == [_GE]


def test_epic_and_cerner_use_the_standard_discovery_path() -> None:
    base = "http://fhir.example/r4/"
    expected = ["http://fhir.example/r4/.well-known/smart-configuration"]
    assert EpicProfile().discovery_paths(base) == expected
    assert CernerProfile().discovery_paths(base) == expected


async def test_upcoming_appointments_drop_sort_on_epic() -> None:
    with respx.mock:
        route = respx.get("http://fhir.example/r4/Appointment").respond(
            200, json={"resourceType": "Bundle", "entry": []}
        )
        fhir = FhirClient(
            "http://fhir.example/r4",
            _Tokens(),
            profile=EpicProfile(),
            sleep=_no_sleep,
        )
        record = PatientRecord(fhir, patient_id="pt-1", clock=lambda: _WHEN)
        async with fhir:
            await record.upcoming_appointments()
    assert "_sort" not in route.calls.last.request.url.params
    assert route.calls.last.request.url.params.get_list("patient") == ["pt-1"]


async def test_appointment_search_gains_lt_on_cerner() -> None:
    with respx.mock:
        route = respx.get("http://fhir.example/r4/Appointment").respond(
            200, json={"resourceType": "Bundle", "entry": []}
        )
        fhir = FhirClient(
            "http://fhir.example/r4",
            _Tokens(),
            profile=CernerProfile(),
            sleep=_no_sleep,
        )
        record = PatientRecord(fhir, patient_id="pt-1", clock=lambda: _WHEN)
        async with fhir:
            await record.search_raw("Appointment", {"date": [_GE]})
    assert route.calls.last.request.url.params.get_list("date") == [_GE, _OPEN_LT]


class _Tokens:
    async def get(self) -> str:
        return "token"

    async def refresh(self, stale: str | None = None) -> str:
        return "token"


async def _no_sleep(_seconds: float) -> None:
    return None


_WHEN = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
