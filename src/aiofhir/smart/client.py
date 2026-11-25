"""SMART authorization code flow."""

from __future__ import annotations

import base64
import hmac
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
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

logger = logging.getLogger(__name__)


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
        logger.info("preparing authorization request for %s", self.client_id)
        logger.debug("authorization scope %s", scope)
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
        logger.info("exchanging authorization code for %s", self.client_id)
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

    async def exchange_refresh(
        self,
        refresh_token: str,
        scope: str,
        previous: TokenSet | None = None,
    ) -> TokenSet:
        """Refresh an access token.

        Refresh responses often omit the launch context, so `patient`, `encounter`,
        `need_patient_banner`, `id_token` and `fhir_user` carry over from `previous`.
        A refresh that names a different patient is refused.
        """
        logger.info("refreshing access token for %s", self.client_id)
        configuration = await self._configuration()
        body = await self._token_request(
            configuration,
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        refreshed = self._token_set(body, scope, refresh_token, require_patient=False)
        if previous is None:
            return refreshed
        return _carry_context(previous, refreshed)

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
        grant = fields["grant_type"]
        logger.debug("token endpoint status=%s grant=%s", response.status_code, grant)
        return _token_body(response)

    def _use_basic(self, configuration: SmartConfiguration) -> bool:
        return bool(self.client_secret) and _supports(configuration, "client_secret_basic")

    def _token_set(
        self,
        body: Mapping[str, Any],
        requested_scope: str,
        refresh_token: str | None = None,
        *,
        require_patient: bool = True,
    ) -> TokenSet:
        patient = body.get("patient")
        wants_patient = "launch/patient" in requested_scope.split()
        if require_patient and wants_patient and not isinstance(patient, str):
            raise TokenError("Token response did not include a patient")
        seconds = _expires_in(body.get("expires_in"))
        raw_id_token = body.get("id_token")
        id_token = raw_id_token if isinstance(raw_id_token, str) else None
        banner = body.get("need_patient_banner")
        raw_scope = body.get("scope")
        scope = raw_scope if isinstance(raw_scope, str) else requested_scope
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise TokenError("Token response did not include an access token")
        logger.debug("token response includes_patient=%s", isinstance(patient, str))
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


# Used when the token response has no usable `expires_in`. Short, so an unknown
# lifetime refreshes early instead of being treated as already expired.
DEFAULT_EXPIRES_IN = 300


def _expires_in(value: object) -> int:
    if isinstance(value, bool):
        return DEFAULT_EXPIRES_IN
    if isinstance(value, int | float):
        seconds = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        seconds = int(value.strip())
    else:
        return DEFAULT_EXPIRES_IN
    return seconds if seconds > 0 else DEFAULT_EXPIRES_IN


def _carry_context(previous: TokenSet, refreshed: TokenSet) -> TokenSet:
    if (
        previous.patient is not None
        and refreshed.patient is not None
        and refreshed.patient != previous.patient
    ):
        raise AuthExpired()
    return replace(
        refreshed,
        patient=refreshed.patient or previous.patient,
        encounter=refreshed.encounter or previous.encounter,
        need_patient_banner=(
            refreshed.need_patient_banner
            if refreshed.need_patient_banner is not None
            else previous.need_patient_banner
        ),
        id_token=refreshed.id_token or previous.id_token,
        fhir_user=refreshed.fhir_user or previous.fhir_user,
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
