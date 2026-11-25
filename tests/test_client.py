from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from aiofhir import FhirClient
from aiofhir.errors import AuthExpired, FhirError

BASE = "http://fhir.example/r4"


class FakeTokens:
    def __init__(self) -> None:
        self.token = "old-token"
        self.refreshes = 0

    async def get(self) -> str:
        return self.token

    async def refresh(self) -> str:
        self.refreshes += 1
        self.token = "new-token"
        return self.token


def _patient(patient_id: str) -> dict[str, str]:
    return {"resourceType": "Patient", "id": patient_id}


async def test_read_sends_bearer_token() -> None:
    with respx.mock:
        route = respx.get(f"{BASE}/Patient/pt-1").respond(200, json=_patient("pt-1"))
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            resource = await fhir.read("Patient", "pt-1")
    assert resource["id"] == "pt-1"
    assert route.calls.last.request.headers["Authorization"] == "Bearer old-token"
    assert route.calls.last.request.headers["Accept"] == "application/fhir+json"


async def test_retries_retryable_statuses_twice_then_succeeds() -> None:
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with respx.mock:
        route = respx.get(f"{BASE}/Patient/pt-1").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(503),
                httpx.Response(200, json=_patient("pt-1")),
            ]
        )
        async with FhirClient(
            BASE,
            FakeTokens(),
            sleep=record_sleep,
            uniform=lambda _a, _b: 1.0,
        ) as fhir:
            resource = await fhir.read("Patient", "pt-1")
    assert resource["id"] == "pt-1"
    assert route.call_count == 3
    assert sleeps == [0.5, 1.0]


async def test_third_retryable_failure_raises() -> None:
    with respx.mock:
        respx.get(f"{BASE}/Patient/pt-1").respond(503)
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            with pytest.raises(FhirError):
                await fhir.read("Patient", "pt-1")


async def test_retry_after_is_honored_and_capped() -> None:
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with respx.mock:
        respx.get(f"{BASE}/Patient/pt-1").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(200, json=_patient("pt-1")),
            ]
        )
        async with FhirClient(BASE, FakeTokens(), sleep=record_sleep) as fhir:
            await fhir.read("Patient", "pt-1")
    assert sleeps == [2.0]

    sleeps.clear()
    with respx.mock:
        respx.get(f"{BASE}/Patient/pt-1").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "3600"}),
                httpx.Response(200, json=_patient("pt-1")),
            ]
        )
        async with FhirClient(BASE, FakeTokens(), sleep=record_sleep) as fhir:
            await fhir.read("Patient", "pt-1")
    assert sleeps == [30.0]


async def test_401_refreshes_once_and_retries() -> None:
    tokens = FakeTokens()
    with respx.mock:
        route = respx.get(f"{BASE}/Patient/pt-1").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json=_patient("pt-1")),
            ]
        )
        async with FhirClient(BASE, tokens, sleep=_no_sleep) as fhir:
            resource = await fhir.read("Patient", "pt-1")
    assert resource["id"] == "pt-1"
    assert tokens.refreshes == 1
    assert route.calls[0].request.headers["Authorization"] == "Bearer old-token"
    assert route.calls[1].request.headers["Authorization"] == "Bearer new-token"


async def test_second_401_raises_auth_expired() -> None:
    tokens = FakeTokens()
    with respx.mock:
        respx.get(f"{BASE}/Patient/pt-1").respond(401)
        async with FhirClient(BASE, tokens, sleep=_no_sleep) as fhir:
            with pytest.raises(AuthExpired):
                await fhir.read("Patient", "pt-1")
    assert tokens.refreshes == 1
    assert str(AuthExpired()) == (
        "Your health record connection expired. Please reconnect to continue."
    )


async def test_operation_outcome_entries_become_warnings() -> None:
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": _patient("pt-1")},
            {
                "resource": {
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "warning", "diagnostics": "incomplete results"}],
                }
            },
        ],
    }
    with respx.mock:
        respx.get(f"{BASE}/Patient").respond(200, json=bundle)
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            result = await fhir.search("Patient", {"name": "Ann"})
    assert [item["id"] for item in result.resources] == ["pt-1"]
    assert result.warnings == ("incomplete results",)


async def test_search_follows_same_origin_pages_and_stops_at_max_pages() -> None:
    def bundle(patient_id: str, nxt: str | None) -> dict[str, object]:
        body: dict[str, object] = {
            "resourceType": "Bundle",
            "entry": [{"resource": _patient(patient_id)}],
        }
        if nxt is not None:
            body["link"] = [{"relation": "next", "url": nxt}]
        return body

    with respx.mock:
        # Register the paged route first. respx keeps the first match, and a path
        # with no query also matches that path when a query is present.
        page_two = respx.get(f"{BASE}/Patient?page=2").respond(
            200, json=bundle("p2", "http://evil.example/Patient?page=3")
        )
        evil = respx.get("http://evil.example/Patient").respond(200, json=bundle("p3", None))
        respx.get(f"{BASE}/Patient").respond(200, json=bundle("p1", f"{BASE}/Patient?page=2"))
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            result = await fhir.search("Patient")
    assert [item["id"] for item in result.resources] == ["p1", "p2"]
    assert page_two.called
    assert not evil.called
    assert any("cross-origin" in warning for warning in result.warnings)


async def test_search_caps_pages_at_ten() -> None:
    def bundle(page: int) -> dict[str, object]:
        nxt = None if page == 12 else f"{BASE}/Observation?page={page + 1}"
        body: dict[str, object] = {
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "Observation", "id": str(page)}}],
        }
        if nxt is not None:
            body["link"] = [{"relation": "next", "url": nxt}]
        return body

    with respx.mock:
        for page in range(2, 13):
            respx.get(f"{BASE}/Observation?page={page}").respond(200, json=bundle(page))
        respx.get(f"{BASE}/Observation").respond(200, json=bundle(1))
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            result = await fhir.search("Observation")
            streamed = [item async for item in fhir.iter_search("Observation")]
    assert [item["id"] for item in result.resources] == [str(page) for page in range(1, 11)]
    assert result.truncated is True
    assert [item["id"] for item in streamed] == [str(page) for page in range(1, 11)]


async def test_concurrency_is_limited_to_four() -> None:
    inflight = 0
    peak = 0
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        if inflight >= 4:
            release.set()
        await release.wait()
        inflight -= 1
        return httpx.Response(200, json=_patient("pt-1"))

    with respx.mock:
        respx.get(url__startswith=f"{BASE}/Patient/").mock(side_effect=handler)
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            await asyncio.gather(*[fhir.read("Patient", str(index)) for index in range(6)])
    assert peak == 4


async def test_timeout_is_applied() -> None:
    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=_patient("pt-1"))

    with respx.mock:
        respx.get(f"{BASE}/Patient/pt-1").mock(side_effect=slow)
        async with FhirClient(BASE, FakeTokens(), timeout=0.05, sleep=_no_sleep) as fhir:
            with pytest.raises(FhirError):
                await fhir.read("Patient", "pt-1")


async def _no_sleep(_seconds: float) -> None:
    return None
