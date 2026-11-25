from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from aiofhir.errors import AuthExpired
from aiofhir.smart import RefreshingToken, SmartClient, TokenSet

BASE = "http://fhir.example/r4"
TOKEN = "http://auth.example/token"
WHEN = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _tokens(**overrides: object) -> TokenSet:
    values: dict[str, object] = {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_at": WHEN + timedelta(hours=2),
        "scope": "patient/*.rs",
        "patient": "pt-1",
        "encounter": None,
        "need_patient_banner": None,
        "id_token": None,
        "fhir_user": None,
    }
    values.update(overrides)
    return TokenSet(**values)  # type: ignore[arg-type]


def _config() -> dict[str, object]:
    return {
        "authorization_endpoint": "http://auth.example/authorize",
        "token_endpoint": TOKEN,
        "code_challenge_methods_supported": ["S256"],
        "capabilities": ["permission-patient", "permission-v2", "permission-offline"],
        "scopes_supported": ["patient/*.rs", "offline_access"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }


async def _smart() -> SmartClient:
    return SmartClient(
        BASE,
        client_id="my-app",
        redirect_uri="http://localhost/callback",
        client_secret="my-secret",
        scope="patient/*.rs",
        clock=lambda: WHEN,
    )


async def test_get_returns_current_token_when_it_is_fresh() -> None:
    with respx.mock:
        route = respx.post(TOKEN).respond(500)
        holder = RefreshingToken(await _smart(), _tokens(), clock=lambda: WHEN)
        assert await holder.get() == "access-1"
    assert not route.called


async def test_get_refreshes_60_seconds_early_and_persists_rotation() -> None:
    saved: list[TokenSet] = []

    async def on_refresh(tokens: TokenSet) -> None:
        saved.append(tokens)

    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        route = respx.post(TOKEN).respond(
            200,
            json={
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
        holder = RefreshingToken(
            await _smart(),
            _tokens(expires_at=WHEN + timedelta(seconds=30)),
            on_refresh=on_refresh,
            clock=lambda: WHEN,
        )
        assert await holder.get() == "access-2"
        await holder.refresh()
    assert route.calls[0].request.content.decode().count("refresh_token=refresh-1") == 1
    assert "refresh_token=refresh-2" in route.calls[1].request.content.decode()
    assert saved[0].refresh_token == "refresh-2"
    assert saved[0].access_token == "access-2"


async def test_concurrent_gets_refresh_once() -> None:
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        calls = 0

        async def token_endpoint(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return httpx.Response(
                200,
                json={
                    "access_token": "access-2",
                    "refresh_token": "refresh-2",
                    "expires_in": 3600,
                },
            )

        respx.post(TOKEN).mock(side_effect=token_endpoint)
        holder = RefreshingToken(
            await _smart(),
            _tokens(expires_at=WHEN + timedelta(seconds=10)),
            clock=lambda: WHEN,
        )
        results = await asyncio.gather(*[holder.get() for _ in range(20)])
    assert results == ["access-2"] * 20
    assert calls == 1


async def test_invalid_grant_raises_auth_expired() -> None:
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        respx.post(TOKEN).respond(400, json={"error": "invalid_grant"})
        holder = RefreshingToken(
            await _smart(),
            _tokens(expires_at=WHEN),
            clock=lambda: WHEN,
        )
        with pytest.raises(AuthExpired) as raised:
            await holder.get()
    assert str(raised.value) == (
        "Your health record connection expired. Please reconnect to continue."
    )
