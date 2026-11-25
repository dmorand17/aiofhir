"""Regressions for refresh context (B1), token lifetime (B3) and 401 storms (C1)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import respx

from aiofhir.errors import AuthExpired
from aiofhir.smart import RefreshingToken, SmartClient, TokenSet

BASE = "http://fhir.example/r4"
TOKEN = "http://auth.example/token"
WHEN = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
LAUNCH_SCOPE = "patient/*.rs launch/patient openid fhirUser offline_access"


def _config() -> dict[str, object]:
    return {
        "authorization_endpoint": "http://auth.example/authorize",
        "token_endpoint": TOKEN,
        "code_challenge_methods_supported": ["S256"],
        "capabilities": ["permission-patient", "permission-v2", "launch-standalone"],
        "scopes_supported": ["patient/*.rs", "launch/patient", "offline_access"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


def _smart() -> SmartClient:
    return SmartClient(BASE, "app", "http://localhost/cb", clock=lambda: WHEN)


def _tokens(**overrides: object) -> TokenSet:
    values: dict[str, object] = {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_at": WHEN + timedelta(hours=1),
        "scope": LAUNCH_SCOPE,
        "patient": "pt-1",
        "encounter": "enc-1",
        "need_patient_banner": True,
        "id_token": "id.token.value",
        "fhir_user": "Patient/pt-1",
    }
    values.update(overrides)
    return TokenSet(**values)  # type: ignore[arg-type]


async def test_refresh_without_patient_keeps_the_launch_context() -> None:
    saved: list[TokenSet] = []

    async def on_refresh(tokens: TokenSet) -> None:
        saved.append(tokens)

    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        respx.post(TOKEN).respond(
            200, json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600}
        )
        holder = RefreshingToken(_smart(), _tokens(), on_refresh=on_refresh, clock=lambda: WHEN)
        assert await holder.refresh() == "access-2"
    refreshed = saved[-1]
    assert refreshed.patient == "pt-1"
    assert refreshed.encounter == "enc-1"
    assert refreshed.need_patient_banner is True
    assert refreshed.fhir_user == "Patient/pt-1"
    assert refreshed.refresh_token == "refresh-2"


async def test_refresh_naming_another_patient_is_refused() -> None:
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        respx.post(TOKEN).respond(
            200, json={"access_token": "access-2", "expires_in": 3600, "patient": "pt-2"}
        )
        holder = RefreshingToken(_smart(), _tokens(), clock=lambda: WHEN)
        with pytest.raises(AuthExpired):
            await holder.refresh()


async def test_code_exchange_still_requires_a_patient_for_launch_patient() -> None:
    from aiofhir.errors import TokenError

    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        respx.post(TOKEN).respond(200, json={"access_token": "a", "expires_in": 3600})
        with pytest.raises(TokenError):
            await _smart().exchange_code("code", "verifier")


@pytest.mark.parametrize("expires_in", [None, "soon", "3600s", 0, -5, True])
async def test_missing_or_bad_expires_in_uses_a_short_default(expires_in: object) -> None:
    body: dict[str, object] = {"access_token": "a", "patient": "pt-1"}
    if expires_in is not None:
        body["expires_in"] = expires_in
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        respx.post(TOKEN).respond(200, json=body)
        tokens = await _smart().exchange_code("code", "verifier")
    assert tokens.expires_at == WHEN + timedelta(seconds=300)
    holder = RefreshingToken(_smart(), tokens, clock=lambda: WHEN)
    assert await holder.get() == "a"


async def test_numeric_string_expires_in_is_accepted() -> None:
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        respx.post(TOKEN).respond(
            200, json={"access_token": "a", "patient": "pt-1", "expires_in": "1800"}
        )
        tokens = await _smart().exchange_code("code", "verifier")
    assert tokens.expires_at == WHEN + timedelta(seconds=1800)


async def test_concurrent_refresh_for_the_same_stale_token_calls_the_server_once() -> None:
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        route = respx.post(TOKEN).respond(
            200, json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600}
        )
        holder = RefreshingToken(_smart(), _tokens(), clock=lambda: WHEN)
        results = await asyncio.gather(*[holder.refresh("access-1") for _ in range(5)])
    assert results == ["access-2"] * 5
    assert route.call_count == 1


def test_from_dict_treats_a_naive_expiry_as_utc() -> None:
    data = _tokens().to_dict()
    data["expires_at"] = "2026-06-01T13:00:00"
    restored = TokenSet.from_dict(data)
    assert restored.expires_at == datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
