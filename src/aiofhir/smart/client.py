"""SMART authorization code flow."""

from __future__ import annotations

import base64
import hmac
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from aiofhir.errors import AuthExpired, StateMismatch, TokenError
from aiofhir.profiles.base import ServerProfile
from aiofhir.profiles.generic import GenericProfile
from aiofhir.smart.discovery import SmartConfiguration, discover
from aiofhir.smart.pkce import challenge_s256, make_state, make_verifier
from aiofhir.smart.scopes import scopes_from_capabilities
from aiofhir.smart.tokens import TokenSet, fhir_user_from_id_token


@dataclass(frozen=True)
class AuthorizationRequest:
    url: str
    state: str
    code_verifier: str


class SmartClient:
    """Discovers a SMART server and performs the authorization-code flow."""

    def __init__(
        self,
        fhir_base_url: str,
        client_id: str,
        redirect_uri: str,
        scope: str | None = None,
        client_secret: str | None = None,
        profile: ServerProfile | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.base = fhir_base_url.rstrip("/")
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.scope = scope
        self.client_secret = client_secret
        self.profile = profile or GenericProfile()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def authorization_request(self) -> AuthorizationRequest:
        configuration = await self._configuration()
        scope = self.scope if self.scope is not None else scopes_from_capabilities(configuration)
        state = make_state()
        verifier = make_verifier()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": scope,
                "state": state,
                "aud": self.base,
                "code_challenge": challenge_s256(verifier),
                "code_challenge_method": "S256",
            }
        )
        return AuthorizationRequest(
            url=f"{configuration.authorization_endpoint}?{query}",
            state=state,
            code_verifier=verifier,
        )

    async def exchange_code(self, code: str, verifier: str) -> TokenSet:
        configuration = await self._configuration()
        scope = self.scope if self.scope is not None else scopes_from_capabilities(configuration)
        body = await self._token_request(
            configuration,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": verifier,
            },
        )
        return self._token_set(body, scope)

    async def exchange_refresh(self, refresh_token: str, scope: str) -> TokenSet:
        configuration = await self._configuration()
        body = await self._token_request(
            configuration,
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        return self._token_set(body, scope, refresh_token)

    async def _configuration(self) -> SmartConfiguration:
        async with httpx.AsyncClient() as http:
            return await discover(self.base, self.profile, http)

    async def _token_request(
        self,
        configuration: SmartConfiguration,
        fields: Mapping[str, str],
    ) -> dict[str, Any]:
        data = {"client_id": self.client_id, **fields}
        headers: dict[str, str] = {}
        if self._use_basic(configuration):
            headers["Authorization"] = _basic(self.client_id, self.client_secret or "")
        elif self.client_secret and _supports(configuration, "client_secret_post"):
            data["client_secret"] = self.client_secret
        async with httpx.AsyncClient() as http:
            response = await http.post(configuration.token_endpoint, data=data, headers=headers)
        return _token_body(response)

    def _use_basic(self, configuration: SmartConfiguration) -> bool:
        return bool(self.client_secret) and _supports(configuration, "client_secret_basic")

    def _token_set(
        self,
        body: Mapping[str, Any],
        requested_scope: str,
        refresh_token: str | None = None,
    ) -> TokenSet:
        patient = body.get("patient")
        if "launch/patient" in requested_scope.split() and not isinstance(patient, str):
            raise TokenError("Token response did not include a patient")
        expires_in = body.get("expires_in")
        seconds = int(expires_in) if isinstance(expires_in, int | float | str) else 0
        raw_id_token = body.get("id_token")
        id_token = raw_id_token if isinstance(raw_id_token, str) else None
        banner = body.get("need_patient_banner")
        raw_scope = body.get("scope")
        scope = raw_scope if isinstance(raw_scope, str) else requested_scope
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise TokenError("Token response did not include an access token")
        raw_refresh = body.get("refresh_token")
        encounter = body.get("encounter")
        return TokenSet(
            access_token=access,
            refresh_token=raw_refresh if isinstance(raw_refresh, str) else refresh_token,
            expires_at=self._clock() + timedelta(seconds=seconds),
            scope=scope,
            patient=patient if isinstance(patient, str) else None,
            encounter=encounter if isinstance(encounter, str) else None,
            need_patient_banner=banner if isinstance(banner, bool) else None,
            id_token=id_token,
            fhir_user=fhir_user_from_id_token(id_token),
        )


def verify_state(*, expected: str, received: str) -> None:
    """Compare OAuth state values without short-circuiting on the first difference."""
    if not expected or not received:
        raise StateMismatch("State mismatch")
    try:
        matches = hmac.compare_digest(expected, received)
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise StateMismatch("State mismatch")


def _supports(configuration: SmartConfiguration, method: str) -> bool:
    return method in configuration.token_endpoint_auth_methods_supported


def _basic(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _token_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("error") == "invalid_grant":
        raise AuthExpired()
    if response.status_code >= 400 or not isinstance(body, dict):
        raise TokenError("Token request failed")
    return body
