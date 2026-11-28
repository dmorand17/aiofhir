"""The scripts in examples/, run against mocked servers."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import respx

from aiofhir.smart import SmartClient, TokenSet

EXAMPLES = Path(__file__).parents[1] / "examples"
FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://fhir.example/r4"
TOKEN = "https://auth.example/token"


def _example(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _smart_configuration(scopes: list[str]) -> dict[str, Any]:
    return {
        "authorization_endpoint": "https://auth.example/authorize",
        "token_endpoint": TOKEN,
        "capabilities": ["permission-patient", "permission-v2", "launch-standalone"],
        "scopes_supported": scopes,
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
    }


def _statement() -> Any:
    return json.loads((FIXTURES / "capability-statement.json").read_text())


def _bundle(*resources: dict[str, Any]) -> dict[str, Any]:
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_standalone_launch_signs_in_and_counts_each_type() -> None:
    example = _example("standalone_launch")
    redirect = f"http://127.0.0.1:{_free_port()}/callback"
    smart = SmartClient(BASE, "my-app", redirect, client_secret="s3cret")
    tasks: list[asyncio.Task[httpx.Response]] = []

    async def browser(url: str) -> httpx.Response:
        state = parse_qs(urlsplit(url).query)["state"][0]
        async with httpx.AsyncClient() as http:
            return await http.get(redirect, params={"code": "the-code", "state": state})

    with respx.mock:
        respx.route(host="127.0.0.1").pass_through()
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200, json=_smart_configuration(["patient/*.rs", "launch/patient"])
        )
        token = respx.post(TOKEN).respond(
            200,
            json={"access_token": "at", "expires_in": 3600, "patient": "pt-1", "scope": "x"},
        )
        respx.get(f"{BASE}/metadata").respond(200, json=_statement())
        observations = respx.get(f"{BASE}/Observation").respond(
            200, json=_bundle({"resourceType": "Observation", "id": "o1"})
        )
        respx.get(url__regex=rf"{BASE}/(?!metadata).*").respond(200, json=_bundle())
        tokens = await example.sign_in(
            smart, lambda url: tasks.append(asyncio.create_task(browser(url)))
        )
        counts = await example.count_by_type(smart, tokens)
    assert (await tasks[0]).status_code == 200
    assert b"code=the-code" in token.calls.last.request.content
    assert counts["Observation"] == (1, False)
    assert counts["Appointment"] == (0, False)
    assert "Medication" not in counts
    assert observations.calls.last.request.url.params["patient"] == "pt-1"


async def test_a_callback_with_the_wrong_state_is_refused() -> None:
    example = _example("standalone_launch")
    redirect = f"http://127.0.0.1:{_free_port()}/callback"
    smart = SmartClient(BASE, "my-app", redirect)
    tasks: list[asyncio.Task[httpx.Response]] = []

    async def browser(url: str) -> httpx.Response:
        async with httpx.AsyncClient() as http:
            return await http.get(redirect, params={"code": "c", "state": "forged"})

    with respx.mock:
        respx.route(host="127.0.0.1").pass_through()
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200, json=_smart_configuration(["patient/*.rs", "launch/patient"])
        )
        token = respx.post(TOKEN).respond(200, json={})
        try:
            await example.sign_in(
                smart, lambda url: tasks.append(asyncio.create_task(browser(url)))
            )
        except Exception as exc:
            refused: Exception | None = exc
        else:
            refused = None
        await tasks[0]
    assert type(refused).__name__ == "StateMismatch"
    assert not token.called


async def test_saved_session_refreshes_and_saves_the_rotated_tokens(tmp_path: Path) -> None:
    example = _example("saved_session")
    path = tmp_path / "tokens.json"
    expired = TokenSet(
        access_token="old",
        refresh_token="refresh-1",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
        scope="patient/*.rs launch/patient offline_access",
        patient="pt-1",
        encounter=None,
        need_patient_banner=None,
        id_token=None,
        fhir_user=None,
    )
    example.save_tokens(path, expired)
    smart = SmartClient(BASE, "my-app", "", client_secret="s3cret")
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200, json=_smart_configuration(["patient/*.rs"])
        )
        respx.post(TOKEN).respond(
            200, json={"access_token": "new", "refresh_token": "refresh-2", "expires_in": 3600}
        )
        respx.get(f"{BASE}/metadata").respond(200, json=_statement())
        patient = respx.get(f"{BASE}/Patient").respond(
            200, json=_bundle({"resourceType": "Patient", "id": "pt-1", "name": [{"text": "Pat"}]})
        )
        found = await example.read_patient(smart, path)
    assert example.display_name(found) == "Pat"
    assert patient.calls.last.request.headers["Authorization"] == "Bearer new"
    saved = example.load_tokens(path)
    assert saved.refresh_token == "refresh-2" and saved.patient == "pt-1"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_backend_services_gets_a_system_token_and_searches() -> None:
    example = _example("backend_services")
    smart = SmartClient(BASE, "my-job", "", client_secret="s3cret")
    configuration = _smart_configuration(["system/*.rs"])
    configuration["capabilities"] = ["permission-v2", "client-confidential-symmetric"]
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=configuration)
        token = respx.post(TOKEN).respond(
            200, json={"access_token": "system-at", "expires_in": 300, "scope": "system/*.rs"}
        )
        search = respx.get(f"{BASE}/Patient").respond(
            200, json=_bundle({"resourceType": "Patient", "id": "a"})
        )
        result, scope = await example.first_page(smart, "Patient")
    assert scope == "system/*.rs"
    assert b"grant_type=client_credentials" in token.calls.last.request.content
    assert search.calls.last.request.headers["Authorization"] == "Bearer system-at"
    assert search.calls.last.request.url.params["_count"] == "10"
    assert [r["id"] for r in result.resources] == ["a"]
