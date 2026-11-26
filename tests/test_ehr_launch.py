"""EHR launch: parse iss and launch, check iss against an allowlist, discover from it."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
import respx

from aiofhir.smart import SmartClient, parse_ehr_launch
from aiofhir.smart.discovery import clear_discovery_cache

ISS = "https://ehr.example/fhir/r4"
TOKEN = "https://ehr.example/oauth2/token"


def _config() -> dict[str, object]:
    return {
        "authorization_endpoint": "https://ehr.example/oauth2/authorize",
        "token_endpoint": TOKEN,
        "code_challenge_methods_supported": ["S256"],
        "capabilities": [
            "launch-ehr",
            "launch-standalone",
            "permission-patient",
            "permission-v2",
            "context-ehr-patient",
        ],
        "token_endpoint_auth_methods_supported": ["none"],
    }


@pytest.fixture(autouse=True)
def _fresh_discovery() -> None:
    clear_discovery_cache()


def _ehr_client() -> SmartClient:
    return SmartClient.for_ehr_launch(
        ISS,
        allowed_issuers=[ISS],
        client_id="ehr-app",
        redirect_uri="https://app.example/callback",
    )


def test_parse_ehr_launch_reads_iss_and_launch() -> None:
    launch = parse_ehr_launch(f"iss={ISS}&launch=xyz123")
    assert launch.iss == ISS and launch.launch == "xyz123"
    assert parse_ehr_launch(f"?launch=abc&iss={ISS}").launch == "abc"


@pytest.mark.parametrize("query", ["launch=xyz", f"iss={ISS}", "", "iss=&launch=x"])
def test_parse_ehr_launch_needs_both_values(query: str) -> None:
    with pytest.raises(ValueError):
        parse_ehr_launch(query)


def test_an_issuer_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(ValueError, match="not an allowed issuer"):
        SmartClient.for_ehr_launch(
            "https://attacker.example/fhir",
            allowed_issuers=[ISS],
            client_id="ehr-app",
            redirect_uri="https://app.example/callback",
        )


def test_an_empty_allowlist_refuses_everything() -> None:
    with pytest.raises(ValueError):
        SmartClient.for_ehr_launch(
            ISS, allowed_issuers=[], client_id="ehr-app", redirect_uri="https://app/cb"
        )


def test_a_trailing_slash_does_not_matter() -> None:
    smart = SmartClient.for_ehr_launch(
        ISS + "/", allowed_issuers=[ISS], client_id="ehr-app", redirect_uri="https://app/cb"
    )
    assert smart.base == ISS


async def test_authorization_request_sends_launch_and_the_launch_scope() -> None:
    smart = _ehr_client()
    with respx.mock:
        discovery = respx.get(f"{ISS}/.well-known/smart-configuration").respond(200, json=_config())
        request = await smart.authorization_request(launch="xyz123")
    assert discovery.called
    query = parse_qs(urlsplit(request.url).query)
    assert query["launch"] == ["xyz123"]
    assert query["aud"] == [ISS]
    scopes = query["scope"][0].split()
    assert "launch" in scopes and "launch/patient" not in scopes
    assert "patient/*.rs" in scopes


async def test_an_ehr_client_needs_the_launch_value() -> None:
    with pytest.raises(ValueError, match="launch"):
        await _ehr_client().authorization_request()


async def test_a_standalone_client_refuses_a_launch_value() -> None:
    smart = SmartClient(ISS, "app", redirect_uri="https://app/cb")
    with pytest.raises(ValueError, match="for_ehr_launch"):
        await smart.authorization_request(launch="xyz")


async def test_ehr_exchange_keeps_the_context_the_ehr_returns() -> None:
    smart = _ehr_client()
    with respx.mock:
        respx.get(f"{ISS}/.well-known/smart-configuration").respond(200, json=_config())
        respx.post(TOKEN).respond(
            200,
            json={
                "access_token": "a",
                "expires_in": 300,
                "patient": "pt-9",
                "encounter": "enc-1",
                "need_patient_banner": False,
                "scope": "launch patient/*.rs",
            },
        )
        tokens = await smart.exchange_code("code", "verifier")
    assert tokens.patient == "pt-9" and tokens.encounter == "enc-1"
    assert tokens.need_patient_banner is False


async def test_ehr_exchange_does_not_force_a_patient() -> None:
    smart = _ehr_client()
    with respx.mock:
        respx.get(f"{ISS}/.well-known/smart-configuration").respond(200, json=_config())
        respx.post(TOKEN).respond(200, json={"access_token": "a", "expires_in": 300})
        tokens = await smart.exchange_code("code", "verifier")
    assert tokens.patient is None
