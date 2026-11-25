"""Seed sign-in stores an access token and never prints it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import respx

from haste.login import SEED_LOGIN_SCOPE, login_access_token, save_env_value

BASE = "http://fhir.example/r4"
TOKEN = "http://auth.example/token"


def _config() -> dict[str, object]:
    return {
        "authorization_endpoint": "http://auth.example/authorize",
        "token_endpoint": TOKEN,
        "code_challenge_methods_supported": ["S256"],
        "capabilities": ["permission-user", "permission-v2", "permission-offline"],
        "scopes_supported": ["user/*.cruds", "openid", "fhirUser", "offline_access"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


def test_save_env_value_sets_haste_token_without_dropping_other_lines(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BASE=http://fhir.example/r4\nHASTE_TOKEN=old-token\n")
    save_env_value(env_file, "HASTE_TOKEN", "access-secret")
    text = env_file.read_text()
    assert "BASE=http://fhir.example/r4" in text
    assert "HASTE_TOKEN=access-secret" in text
    assert "old-token" not in text


def test_save_env_value_appends_when_the_key_is_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BASE=http://fhir.example/r4\n")
    save_env_value(env_file, "HASTE_TOKEN", "access-secret")
    assert env_file.read_text().splitlines() == [
        "BASE=http://fhir.example/r4",
        "HASTE_TOKEN=access-secret",
    ]


async def test_login_access_token_uses_the_existing_client_and_write_scope() -> None:
    seen: list[str] = []

    async def browse(url: str) -> None:
        seen.append(url)
        query = parse_qs(urlsplit(url).query)
        callback = "http://127.0.0.1:8876/callback?" + urlencode(
            {"code": "auth-code", "state": query["state"][0]}
        )

        def hit() -> int:
            with urlopen(callback) as response:
                return int(response.status)

        assert await asyncio.to_thread(hit) == 200

    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(200, json=_config())
        respx.post(TOKEN).respond(
            200,
            json={"access_token": "access-secret", "expires_in": 3600, "token_type": "Bearer"},
        )
        token = await login_access_token(
            BASE,
            "existing-client",
            None,
            scope=SEED_LOGIN_SCOPE,
            open_browser=browse,
            port=8876,
        )
    query = parse_qs(urlsplit(seen[0]).query)
    assert query["client_id"] == ["existing-client"]
    assert query["scope"] == [SEED_LOGIN_SCOPE]
    assert "launch/patient" not in SEED_LOGIN_SCOPE
    assert token == "access-secret"
