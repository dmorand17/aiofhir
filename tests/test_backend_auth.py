"""private_key_jwt client authentication and the client_credentials grant."""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from aiofhir.errors import TokenError
from aiofhir.smart import ClientAssertionKey, SmartClient
from aiofhir.smart.discovery import clear_discovery_cache

BASE = "https://fhir.example/r4"
STATEMENT = json.loads(
    (Path(__file__).parent / "fixtures" / "capability-statement.json").read_text()
)
TOKEN = "https://auth.example/token"


def _config(auth_methods: list[str], scopes: list[str] | None = None) -> dict[str, object]:
    config: dict[str, object] = {
        "authorization_endpoint": "https://auth.example/authorize",
        "token_endpoint": TOKEN,
        "code_challenge_methods_supported": ["S256"],
        "capabilities": ["permission-patient", "permission-v2", "client-confidential-asymmetric"],
        "token_endpoint_auth_methods_supported": auth_methods,
    }
    if scopes is not None:
        config["scopes_supported"] = scopes
    return config


def _pem(private_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey) -> bytes:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@pytest.fixture(autouse=True)
def _fresh_discovery() -> None:
    clear_discovery_cache()


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def _token_response() -> dict[str, object]:
    return {"access_token": "system-access", "expires_in": 300, "scope": "system/*.rs"}


@pytest.mark.parametrize("algorithm", ["RS384", "ES384"])
async def test_private_key_jwt_signs_a_short_lived_client_assertion(algorithm: str) -> None:
    private_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey
    if algorithm == "RS384":
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    else:
        private_key = ec.generate_private_key(ec.SECP384R1())
    key = ClientAssertionKey(_pem(private_key), key_id="key-1", algorithm=algorithm)
    smart = SmartClient(BASE, "backend-app", redirect_uri="", private_key=key)
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200, json=_config(["private_key_jwt"])
        )
        route = respx.post(TOKEN).respond(200, json=_token_response())
        tokens = await smart.client_credentials()
    request = route.calls.last.request
    form = _form(request)
    assert "Authorization" not in request.headers
    assert "client_secret" not in form
    assert form["grant_type"] == "client_credentials"
    assert form["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    assertion = form["client_assertion"]
    header = jwt.get_unverified_header(assertion)
    assert header["alg"] == algorithm and header["kid"] == "key-1" and header["typ"] == "JWT"
    claims = jwt.decode(assertion, private_key.public_key(), algorithms=[algorithm], audience=TOKEN)
    assert claims["iss"] == claims["sub"] == "backend-app"
    assert 0 < claims["exp"] - time.time() <= 300
    assert len(claims["jti"]) >= 16
    assert tokens.access_token == "system-access" and tokens.patient is None


async def test_each_assertion_has_a_fresh_jti() -> None:
    private_key = ec.generate_private_key(ec.SECP384R1())
    key = ClientAssertionKey(_pem(private_key), key_id="k", algorithm="ES384")
    smart = SmartClient(BASE, "backend-app", redirect_uri="", private_key=key)
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200, json=_config(["private_key_jwt"])
        )
        route = respx.post(TOKEN).respond(200, json=_token_response())
        await smart.client_credentials()
        await smart.client_credentials()
    jtis = {
        jwt.decode(_form(call.request)["client_assertion"], options={"verify_signature": False})[
            "jti"
        ]
        for call in route.calls
    }
    assert len(jtis) == 2


async def test_private_key_jwt_is_refused_when_the_server_does_not_list_it() -> None:
    key = ClientAssertionKey(
        _pem(ec.generate_private_key(ec.SECP384R1())), key_id="k", algorithm="ES384"
    )
    smart = SmartClient(BASE, "backend-app", redirect_uri="", private_key=key)
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200, json=_config(["client_secret_basic"])
        )
        token = respx.post(TOKEN).respond(200, json=_token_response())
        with pytest.raises(TokenError, match="private_key_jwt"):
            await smart.client_credentials()
    assert not token.called


def test_an_unsupported_algorithm_is_refused() -> None:
    with pytest.raises(ValueError, match="RS384 or ES384"):
        ClientAssertionKey(b"pem", key_id="k", algorithm="HS256")


def test_the_key_repr_hides_the_private_key() -> None:
    pem = _pem(ec.generate_private_key(ec.SECP384R1()))
    key = ClientAssertionKey(pem, key_id="k", algorithm="ES384")
    assert pem.decode() not in repr(key) and "PRIVATE" not in repr(key)


async def test_client_credentials_derives_a_system_read_scope() -> None:
    smart = SmartClient(BASE, "backend-app", redirect_uri="", client_secret="s3cret")
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200, json=_config(["client_secret_basic"])
        )
        route = respx.post(TOKEN).respond(200, json=_token_response())
        await smart.client_credentials()
    request = route.calls.last.request
    assert request.headers["Authorization"].startswith("Basic ")
    assert _form(request)["scope"] == "system/*.rs"


async def test_client_credentials_uses_listed_per_resource_system_scopes() -> None:
    smart = SmartClient(BASE, "backend-app", redirect_uri="", client_secret="s3cret")
    listed = ["system/Patient.rs", "system/Observation.rs", "patient/*.rs"]
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200, json=_config(["client_secret_basic"], scopes=listed)
        )
        respx.get(f"{BASE}/metadata").respond(200, json=STATEMENT)
        route = respx.post(TOKEN).respond(200, json=_token_response())
        await smart.client_credentials()
    assert _form(route.calls.last.request)["scope"] == "system/Patient.rs system/Observation.rs"


async def test_client_credentials_explicit_scope_wins() -> None:
    smart = SmartClient(BASE, "backend-app", redirect_uri="", client_secret="s3cret")
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200, json=_config(["client_secret_basic"])
        )
        route = respx.post(TOKEN).respond(200, json=_token_response())
        await smart.client_credentials(scope="system/*.cruds")
    assert _form(route.calls.last.request)["scope"] == "system/*.cruds"


async def test_client_credentials_needs_a_confidential_client() -> None:
    smart = SmartClient(BASE, "public-app", redirect_uri="")
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config(["none"]))
        token = respx.post(TOKEN).respond(200, json=_token_response())
        with pytest.raises(TokenError, match="confidential"):
            await smart.client_credentials()
    assert not token.called
