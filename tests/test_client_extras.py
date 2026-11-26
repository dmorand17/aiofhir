"""FhirClient: vread, cached /metadata, POST _search for long queries, request hooks."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
import respx

from aiofhir import FhirClient
from aiofhir.client import RequestEvent
from aiofhir.errors import FhirError
from aiofhir.profiles import HasteProfile

BASE = "http://fhir.example/r4"


class FakeTokens:
    async def get(self) -> str:
        return "secret-token"

    async def refresh(self, stale: str | None = None) -> str:
        return "secret-token"


async def _no_sleep(_: float) -> None:
    return None


def _bundle(ids: list[str]) -> dict[str, object]:
    return {
        "resourceType": "Bundle",
        "entry": [{"resource": {"resourceType": "Observation", "id": i}} for i in ids],
    }


def _long_codes(count: int = 200) -> str:
    return ",".join(f"http://loinc.org|{1000 + n}-{n % 10}" for n in range(count))


async def test_vread_reads_one_version() -> None:
    with respx.mock:
        route = respx.get(f"{BASE}/Observation/o1/_history/3").respond(
            200, json={"resourceType": "Observation", "id": "o1", "meta": {"versionId": "3"}}
        )
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            body = await fhir.vread("Observation", "o1", "3")
    assert route.called and body["meta"] == {"versionId": "3"}


@pytest.mark.parametrize("version", ["", "..", "3/../4", "a b"])
async def test_vread_refuses_a_bad_version(version: str) -> None:
    async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
        with pytest.raises(ValueError):
            await fhir.vread("Observation", "o1", version)


async def test_capabilities_are_fetched_once() -> None:
    with respx.mock:
        route = respx.get(f"{BASE}/metadata").respond(
            200, json={"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1"}
        )
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            first = await fhir.capabilities()
            second = await fhir.capabilities()
    assert first["fhirVersion"] == "4.0.1" and first == second
    assert route.call_count == 1


async def test_a_short_search_stays_a_get() -> None:
    with respx.mock:
        get = respx.get(f"{BASE}/Observation").respond(200, json=_bundle(["o1"]))
        post = respx.post(f"{BASE}/Observation/_search").respond(200, json=_bundle([]))
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            await fhir.search("Observation", {"patient": "pt-1", "code": "4548-4"})
    assert get.called and not post.called


async def test_a_long_search_is_posted_to_search_as_a_form() -> None:
    codes = _long_codes()
    with respx.mock:
        post = respx.post(f"{BASE}/Observation/_search").respond(
            200,
            json={
                **_bundle(["o1"]),
                "link": [{"relation": "next", "url": f"{BASE}/Observation?page=2"}],
            },
        )
        page_two = respx.get(f"{BASE}/Observation", params={"page": "2"}).respond(
            200, json=_bundle(["o2"])
        )
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep) as fhir:
            result = await fhir.search("Observation", {"patient": "pt-1", "code": codes})
    request = post.calls.last.request
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.url.query == b""
    form = parse_qs(request.content.decode())
    assert form["patient"] == ["pt-1"] and form["code"] == [codes]
    assert page_two.called
    assert [r["id"] for r in result.resources] == ["o1", "o2"]


async def test_offset_paging_of_a_long_search_keeps_posting() -> None:
    codes = _long_codes()

    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(parse_qs(request.content.decode()).get("_offset", ["0"])[0])
        return httpx.Response(
            200, json=_bundle([f"o{offset}", f"o{offset + 1}"][: 2 if offset == 0 else 1])
        )

    with respx.mock:
        post = respx.post(f"{BASE}/Observation/_search").mock(side_effect=respond)
        async with FhirClient(BASE, FakeTokens(), profile=HasteProfile(), sleep=_no_sleep) as fhir:
            result = await fhir.search(
                "Observation", {"patient": "pt-1", "code": codes, "_count": "2"}
            )
    assert post.call_count == 2
    assert [r["id"] for r in result.resources] == ["o0", "o1", "o2"]


async def test_hooks_see_redacted_requests_and_every_attempt() -> None:
    events: list[RequestEvent] = []
    with respx.mock:
        respx.get(f"{BASE}/Observation").mock(
            side_effect=[httpx.Response(429), httpx.Response(200, json=_bundle(["o1"]))]
        )
        async with FhirClient(
            BASE, FakeTokens(), sleep=_no_sleep, on_request=events.append
        ) as fhir:
            await fhir.search("Observation", {"patient": "pt-secret", "category": "laboratory"})
    assert [(e.method, e.status, e.attempt) for e in events] == [
        ("GET", 429, 1),
        ("GET", 200, 2),
    ]
    for event in events:
        text = repr(event)
        assert "pt-secret" not in text and "secret-token" not in text
        assert event.params["category"] == ["laboratory"]
        assert event.elapsed_ms >= 0


async def test_hooks_can_be_async_and_hide_ids_in_paths() -> None:
    seen: list[str] = []

    async def record(event: RequestEvent) -> None:
        seen.append(event.url)

    with respx.mock:
        respx.get(f"{BASE}/Patient/pt-secret").respond(200, json={"resourceType": "Patient"})
        async with FhirClient(BASE, FakeTokens(), sleep=_no_sleep, on_request=record) as fhir:
            await fhir.read("Patient", "pt-secret")
    assert seen == [f"{BASE}/Patient/[id]"]


async def test_hooks_see_a_failed_request() -> None:
    events: list[RequestEvent] = []
    with respx.mock:
        respx.get(f"{BASE}/Observation/o1").mock(side_effect=httpx.ConnectError("down"))
        async with FhirClient(
            BASE, FakeTokens(), sleep=_no_sleep, on_request=events.append
        ) as fhir:
            with pytest.raises(FhirError):
                await fhir.read("Observation", "o1")
    # One attempt and two retries, each reported.
    assert len(events) == 3
    assert [event.attempt for event in events] == [1, 2, 3]
    assert all(event.status is None and event.error == "request failed" for event in events)
