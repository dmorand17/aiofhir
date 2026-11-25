from __future__ import annotations

import httpx
import respx

from haste.login import capture_callback, run_smoke

BASE = "http://fhir.example/r4"
TOKEN = "http://auth.example/token"


def _config() -> dict[str, object]:
    return {
        "authorization_endpoint": "http://auth.example/authorize",
        "token_endpoint": TOKEN,
        "code_challenge_methods_supported": ["S256"],
        "capabilities": [
            "permission-patient",
            "permission-v2",
            "launch-standalone",
            "sso-openid-connect",
            "permission-offline",
        ],
        "scopes_supported": [
            "patient/*.rs",
            "openid",
            "fhirUser",
            "launch/patient",
            "offline_access",
        ],
        "token_endpoint_auth_methods_supported": ["none"],
    }


async def test_capture_callback_checks_state_and_returns_the_code() -> None:
    server, future = await capture_callback("state-1", port=0, host="127.0.0.1")
    sockets = server.sockets
    assert sockets is not None
    port = sockets[0].getsockname()[1]
    async with server:
        async with httpx.AsyncClient() as http:
            response = await http.get(
                f"http://127.0.0.1:{port}/callback",
                params={"code": "auth-code", "state": "state-1"},
            )
        assert response.status_code == 200
        assert await future == "auth-code"


async def test_run_smoke_prints_helpers_forces_refresh_and_hides_tokens() -> None:
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        token_route = respx.post(TOKEN).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "access_token": "access-secret",
                        "refresh_token": "refresh-secret",
                        "expires_in": 3600,
                        "patient": "pt-1",
                        "scope": "launch/patient",
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "access_token": "access-2",
                        "refresh_token": "refresh-2",
                        "expires_in": 3600,
                        "patient": "pt-1",
                    },
                ),
            ]
        )
        for resource in (
            "Appointment",
            "MedicationRequest",
            "AllergyIntolerance",
            "Condition",
            "Observation",
        ):
            respx.get(f"{BASE}/{resource}").respond(
                200,
                json={"resourceType": "Bundle", "entry": []},
            )
        report = await run_smoke(BASE, "aiofhir-smoke", None, "auth-code", "verifier-1")
    assert "patient: pt-1" in report
    assert "upcoming_appointments" in report
    assert "recent_labs" in report
    assert "access-secret" not in report
    assert "refresh-secret" not in report
    assert token_route.call_count == 2
    assert "refresh: ok, patient kept" in report


async def test_callback_error_is_reported_instead_of_hanging() -> None:
    import pytest

    from haste.login import SignInError

    server, future = await capture_callback("state-1", port=0, host="127.0.0.1")
    sockets = server.sockets
    assert sockets is not None
    port = sockets[0].getsockname()[1]
    async with server:
        async with httpx.AsyncClient() as http:
            response = await http.get(
                f"http://127.0.0.1:{port}/callback",
                params={"error": "invalid_scope", "state": "state-1"},
            )
        assert response.status_code == 400
        with pytest.raises(SignInError, match="invalid_scope"):
            await future


async def test_wait_for_code_times_out() -> None:
    import asyncio

    import pytest

    from haste.login import SignInError, wait_for_code

    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    with pytest.raises(SignInError):
        await wait_for_code(future, timeout=0.01)


async def test_run_smoke_explains_a_token_without_a_patient() -> None:
    import pytest

    from haste.login import SignInError

    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        respx.post(TOKEN).respond(
            200, json={"access_token": "a", "expires_in": 3600, "scope": "patient/*.rs"}
        )
        with pytest.raises(SignInError, match="patient user"):
            await run_smoke(BASE, "aiofhir-smoke", None, "auth-code", "verifier-1")


async def test_run_smoke_hands_out_the_exchanged_and_refreshed_tokens() -> None:
    from aiofhir.smart import TokenSet

    seen: list[TokenSet] = []

    async def keep(tokens: TokenSet) -> None:
        seen.append(tokens)

    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        respx.post(TOKEN).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "access_token": "access-1",
                        "refresh_token": "refresh-1",
                        "expires_in": 3600,
                        "patient": "pt-1",
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "access_token": "access-2",
                        "refresh_token": "refresh-2",
                        "expires_in": 3600,
                        "patient": "pt-1",
                    },
                ),
            ]
        )
        for resource in (
            "Appointment",
            "MedicationRequest",
            "AllergyIntolerance",
            "Condition",
            "Observation",
        ):
            respx.get(f"{BASE}/{resource}").respond(
                200, json={"resourceType": "Bundle", "entry": []}
            )
        await run_smoke(BASE, "aiofhir-smoke", None, "auth-code", "verifier-1", on_tokens=keep)
    assert [t.refresh_token for t in seen] == ["refresh-1", "refresh-2"]
    assert seen[-1].patient == "pt-1"


def test_saved_tokens_round_trip_and_are_owner_only(tmp_path: object) -> None:
    import stat
    from pathlib import Path

    from aiofhir.smart import TokenSet
    from haste.login import load_tokens, save_tokens

    assert isinstance(tmp_path, Path)
    path = tmp_path / ".tokens.json"
    path.write_text("{}")
    path.chmod(0o644)
    tokens = TokenSet.from_dict(
        {
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": "2026-01-01T00:00:00+00:00",
            "patient": "pt-1",
            "scope": "s",
        }
    )
    save_tokens(path, tokens)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = load_tokens(path)
    assert loaded.patient == "pt-1"
    assert loaded.refresh_token == "r"


async def test_stray_connections_do_not_end_the_sign_in() -> None:
    import asyncio

    server, future = await capture_callback("state-1", port=0, host="127.0.0.1")
    sockets = server.sockets
    assert sockets is not None
    port = sockets[0].getsockname()[1]
    async with server:
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        async with httpx.AsyncClient() as http:
            stray = await http.get(f"http://127.0.0.1:{port}/favicon.ico")
            await asyncio.sleep(0.05)
            assert not future.done()
            await http.get(
                f"http://127.0.0.1:{port}/callback",
                params={"code": "auth-code", "state": "state-1"},
            )
        assert stray.status_code == 404
        assert await future == "auth-code"
