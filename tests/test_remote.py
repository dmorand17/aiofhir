"""RemoteRecord against the reference backend, over ASGI."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from aiofhir import RECORD_HELPERS, AuthExpired, PatientRecord, Record, RemoteRecord
from aiofhir.errors import FhirError
from aiofhir.server import RECORD_METHODS, EncryptedStore, create_app
from aiofhir.smart import TokenSet
from aiofhir.smart.discovery import clear_discovery_cache
from aiofhir.summary import Summary

FHIR = "https://fhir.example/r4"
BACKEND = "http://backend"
SERVICE = "service-secret"
SUBJECT = "user:42"
HELPERS = (
    "upcoming_appointments",
    "active_medications",
    "allergies",
    "conditions",
    "recent_labs",
    "immunizations",
    "vital_signs",
    "past_visits",
    "procedures",
    "documents",
)


def _tokens() -> TokenSet:
    return TokenSet(
        access_token="access-1",
        refresh_token="refresh-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope="patient/*.rs launch/patient",
        patient="pt-1",
        encounter=None,
        need_patient_banner=None,
        id_token=None,
        fhir_user=None,
    )


@pytest.fixture(autouse=True)
def _fresh_discovery() -> None:
    clear_discovery_cache()


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    store = EncryptedStore(Fernet.generate_key())
    store.put(SUBJECT, _tokens())
    app = create_app(
        fhir_base_url=FHIR,
        client_id="aiofhir-app",
        redirect_uri=f"{BACKEND}/auth/callback",
        service_token=SERVICE,
        store=store,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        yield client


def _mock_fhir(router: respx.MockRouter) -> None:
    allergy = {
        "resourceType": "AllergyIntolerance",
        "id": "al-1",
        "patient": {"reference": "Patient/pt-1"},
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"text": "Peanut"},
    }
    router.get(f"{FHIR}/AllergyIntolerance").respond(
        200, json={"resourceType": "Bundle", "entry": [{"resource": allergy}]}
    )
    router.get(f"{FHIR}/Appointment").respond(200, json={"resourceType": "Bundle", "entry": []})


def test_both_records_match_the_record_protocol() -> None:
    assert sorted(RECORD_HELPERS) == sorted(HELPERS)
    for name in RECORD_HELPERS:
        expected = inspect.signature(getattr(Record, name))
        for cls in (PatientRecord, RemoteRecord):
            assert inspect.signature(getattr(cls, name)) == expected, f"{cls.__name__}.{name}"


def test_server_allowlist_is_derived_from_the_protocol() -> None:
    assert set(RECORD_METHODS) == set(RECORD_HELPERS)
    assert RECORD_METHODS["upcoming_appointments"] == frozenset({"days"})
    assert RECORD_METHODS["allergies"] == frozenset()


def _uses_any_record(record: Record) -> None:
    """mypy checks that both classes satisfy Record."""


def test_either_record_can_stand_in_for_record() -> None:
    _uses_any_record(RemoteRecord(BACKEND, SUBJECT, token=SERVICE))
    assert isinstance(RemoteRecord(BACKEND, SUBJECT, token=SERVICE), Record)


async def test_a_patient_record_is_a_record() -> None:
    from aiofhir import FhirClient

    class _Tokens:
        async def get(self) -> str:
            return "t"

        async def refresh(self, stale: str | None = None) -> str:
            return "t"

    async with FhirClient(FHIR, _Tokens()) as fhir:
        local = PatientRecord(fhir, patient_id="pt-1")
        _uses_any_record(local)
        assert isinstance(local, Record)


async def test_remote_record_returns_a_summary(http: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_fhir(router)
        record = RemoteRecord(BACKEND, SUBJECT, token=SERVICE, client=http)
        summary = await record.allergies()
    assert isinstance(summary, Summary)
    assert [item.text for item in summary.items] == ["Peanut"]
    assert str(summary.items[0].citation) == "AllergyIntolerance/al-1"


async def test_days_is_passed_through(http: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        appointments = router.get(f"{FHIR}/Appointment").respond(
            200, json={"resourceType": "Bundle", "entry": []}
        )
        record = RemoteRecord(BACKEND, SUBJECT, token=SERVICE, client=http)
        summary = await record.upcoming_appointments(days=30)
    assert summary.kind == "upcoming_appointments"
    dates = appointments.calls.last.request.url.params.get_list("date")
    start = datetime.fromisoformat(dates[0][2:].replace("Z", "+00:00"))
    end = datetime.fromisoformat(dates[1][2:].replace("Z", "+00:00"))
    assert (end - start).days == 30


async def test_unknown_subject_raises_auth_expired(http: httpx.AsyncClient) -> None:
    record = RemoteRecord(BACKEND, "user:nobody", token=SERVICE, client=http)
    with pytest.raises(AuthExpired):
        await record.allergies()


async def test_wrong_service_token_is_not_auth_expired(http: httpx.AsyncClient) -> None:
    record = RemoteRecord(BACKEND, SUBJECT, token="wrong", client=http)
    with pytest.raises(FhirError) as caught:
        await record.allergies()
    assert caught.value.status == 401


def test_repr_hides_the_service_token() -> None:
    record = RemoteRecord(BACKEND, SUBJECT, token=SERVICE)
    assert SERVICE not in repr(record)
