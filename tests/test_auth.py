from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
import respx

from aiofhir.errors import StateMismatch, TokenError
from aiofhir.smart import SmartClient, verify_state
from aiofhir.smart.pkce import challenge_s256

BASE = "http://fhir.example/r4"
WELL_KNOWN = f"{BASE}/.well-known/smart-configuration"
TOKEN = "http://auth.example/token"
WHEN = datetime(2026, 6, 1, tzinfo=UTC)


def _jwt(payload: dict[str, str]) -> str:
    def segment(value: dict[str, str]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'none'})}.{segment(payload)}.id-token-secret"


def _config(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
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
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
    }
    body.update(overrides)
    return body


def test_s256_matches_rfc7636_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert challenge_s256(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verify_state_rejects_mismatch_and_accepts_match() -> None:
    verify_state(expected="state-1", received="state-1")
    with pytest.raises(StateMismatch):
        verify_state(expected="state-1", received="state-2")
    with pytest.raises(StateMismatch):
        verify_state(expected="", received="state-1")
    with pytest.raises(StateMismatch):
        verify_state(expected="state-1", received="")


async def test_authorization_request_sends_pkce_state_and_aud() -> None:
    with respx.mock:
        respx.get(WELL_KNOWN).respond(200, json=_config())
        smart = SmartClient(
            BASE + "/",
            client_id="my-app",
            redirect_uri="http://localhost:8765/callback",
            scope=None,
            clock=lambda: WHEN,
        )
        req = await smart.authorization_request()
    query = parse_qs(urlsplit(req.url).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["my-app"]
    assert query["redirect_uri"] == ["http://localhost:8765/callback"]
    assert query["aud"] == [BASE]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [challenge_s256(req.code_verifier)]
    assert query["scope"] == ["patient/*.rs launch/patient openid fhirUser offline_access"]
    assert query["state"] == [req.state]
    assert 43 <= len(req.code_verifier) <= 128
    assert 43 <= len(req.state) <= 128


async def test_explicit_scope_replaces_derived_scope() -> None:
    with respx.mock:
        respx.get(WELL_KNOWN).respond(200, json=_config())
        smart = SmartClient(
            BASE,
            client_id="my-app",
            redirect_uri="http://localhost:8765/callback",
            scope="openid patient/Patient.read",
            clock=lambda: WHEN,
        )
        req = await smart.authorization_request()
    query = parse_qs(urlsplit(req.url).query)
    assert query["scope"] == ["openid patient/Patient.read"]


async def test_exchange_uses_basic_auth_and_returns_launch_context() -> None:
    with respx.mock:
        respx.get(WELL_KNOWN).respond(200, json=_config())
        token_route = respx.post(TOKEN).respond(
            200,
            json={
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": "patient/*.rs launch/patient",
                "patient": "pt-1",
                "encounter": "enc-1",
                "need_patient_banner": True,
                "id_token": _jwt({"fhirUser": "Patient/pt-1"}),
            },
        )
        smart = SmartClient(
            BASE,
            client_id="my-app",
            redirect_uri="http://localhost:8765/callback",
            client_secret="my-secret",
            clock=lambda: WHEN,
        )
        tokens = await smart.exchange_code("auth-code", "verifier-1")
    request = token_route.calls.last.request
    assert request.headers["Authorization"] == "Basic bXktYXBwOm15LXNlY3JldA=="
    body = request.content.decode()
    assert "client_secret" not in body
    assert "grant_type=authorization_code" in body
    assert "code=auth-code" in body
    assert "code_verifier=verifier-1" in body
    assert tokens.patient == "pt-1"
    assert tokens.encounter == "enc-1"
    assert tokens.need_patient_banner is True
    assert tokens.fhir_user == "Patient/pt-1"
    assert tokens.expires_at == datetime(2026, 6, 1, 1, 0, tzinfo=UTC)
    assert "access-1" not in repr(tokens)
    assert "refresh-1" not in repr(tokens)
    assert "id-token-secret" not in repr(tokens)
    restored = type(tokens).from_dict(tokens.to_dict())
    assert restored == tokens


async def test_exchange_uses_post_auth_when_basic_is_absent() -> None:
    with respx.mock:
        respx.get(WELL_KNOWN).respond(
            200,
            json=_config(token_endpoint_auth_methods_supported=["client_secret_post"]),
        )
        token_route = respx.post(TOKEN).respond(
            200,
            json={
                "access_token": "access-1",
                "expires_in": 60,
                "patient": "pt-1",
            },
        )
        smart = SmartClient(
            BASE,
            client_id="my-app",
            redirect_uri="http://localhost:8765/callback",
            client_secret="my-secret",
            scope="launch/patient",
            clock=lambda: WHEN,
        )
        await smart.exchange_code("auth-code", "verifier-1")
    request = token_route.calls.last.request
    assert "Authorization" not in request.headers
    assert "client_secret=my-secret" in request.content.decode()


async def test_missing_patient_raises_when_launch_patient_was_requested() -> None:
    with respx.mock:
        respx.get(WELL_KNOWN).respond(200, json=_config())
        respx.post(TOKEN).respond(
            200,
            json={"access_token": "access-secret", "expires_in": 60, "token_type": "Bearer"},
        )
        smart = SmartClient(
            BASE,
            client_id="my-app",
            redirect_uri="http://localhost:8765/callback",
            scope="launch/patient",
            clock=lambda: WHEN,
        )
        with pytest.raises(TokenError) as raised:
            await smart.exchange_code("auth-code", "verifier-1")
    assert "access-secret" not in str(raised.value)


async def test_a_token_error_names_the_servers_error_and_is_redacted() -> None:
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        respx.post(TOKEN).respond(
            400,
            json={
                "error": "invalid_request",
                "error_description": "Bad verifier for Patient/pt-secret",
            },
        )
        smart = SmartClient(
            BASE,
            client_id="my-app",
            redirect_uri="http://localhost:8765/callback",
            scope="launch/patient",
            clock=lambda: WHEN,
        )
        with pytest.raises(TokenError) as raised:
            await smart.exchange_code("auth-code", "verifier-1")
    message = str(raised.value)
    assert "(400)" in message and "invalid_request" in message and "Bad verifier" in message
    assert "pt-secret" not in message
