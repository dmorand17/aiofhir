"""Dropped connections and timeouts are retried for reads and searches."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from aiofhir import FhirClient
from aiofhir.errors import FhirError

BASE = "http://fhir.example/r4"
BUNDLE = {"resourceType": "Bundle", "type": "searchset", "entry": []}


class _Tokens:
    async def get(self) -> str:
        return "tok"

    async def refresh(self, stale: str | None = None) -> str:
        return "tok"


def _client(sleeps: list[float]) -> FhirClient:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return FhirClient(BASE, _Tokens(), sleep=sleep, uniform=lambda _a, _b: 1.0)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("down"),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("closed"),
        httpx.ReadTimeout("slow"),
        httpx.PoolTimeout("busy"),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_read_recovers_after_a_transport_error(error: Exception) -> None:
    sleeps: list[float] = []
    with respx.mock:
        route = respx.get(f"{BASE}/Patient/p1").mock(
            side_effect=[error, httpx.Response(200, json={"resourceType": "Patient", "id": "p1"})]
        )
        async with _client(sleeps) as fhir:
            resource = await fhir.read("Patient", "p1")
    assert resource["id"] == "p1"
    assert route.call_count == 2
    assert sleeps == [0.5]


async def test_gives_up_after_two_retries() -> None:
    sleeps: list[float] = []
    with respx.mock:
        route = respx.get(f"{BASE}/Patient/p1").mock(side_effect=httpx.ConnectError("down"))
        async with _client(sleeps) as fhir:
            with pytest.raises(FhirError) as caught:
                await fhir.read("Patient", "p1")
    assert route.call_count == 3
    assert sleeps == [0.5, 1.0]
    assert caught.value.status == 0


async def test_post_search_is_retried() -> None:
    sleeps: list[float] = []
    long_value = "x" * 2100
    with respx.mock:
        route = respx.post(f"{BASE}/Observation/_search").mock(
            side_effect=[httpx.ConnectError("down"), httpx.Response(200, json=BUNDLE)]
        )
        async with _client(sleeps) as fhir:
            result = await fhir.search("Observation", {"code": long_value})
    assert result.resources == ()
    assert route.call_count == 2


async def test_asyncio_timeout_is_retried_then_raises() -> None:
    attempts: list[int] = []

    async def slow(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=BUNDLE)

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with respx.mock:
        respx.get(f"{BASE}/Observation").mock(side_effect=slow)
        async with FhirClient(BASE, _Tokens(), timeout=0.02, sleep=sleep) as fhir:
            with pytest.raises(FhirError, match="timed out"):
                await fhir.search("Observation")
    assert len(attempts) == 3
    assert len(sleeps) == 2


async def test_retry_budget_is_shared_with_status_retries() -> None:
    sleeps: list[float] = []
    with respx.mock:
        route = respx.get(f"{BASE}/Patient/p1").mock(
            side_effect=[
                httpx.Response(503),
                httpx.ConnectError("down"),
                httpx.Response(503),
            ]
        )
        async with _client(sleeps) as fhir:
            with pytest.raises(FhirError):
                await fhir.read("Patient", "p1")
    assert route.call_count == 3
