"""Access tokens and refresh."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiofhir.errors import AuthExpired


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scope: str
    patient: str | None
    encounter: str | None
    need_patient_banner: bool | None
    id_token: str | None
    fhir_user: str | None

    def __repr__(self) -> str:
        return f"TokenSet(patient={self.patient!r}, expires_at={self.expires_at!r})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat(),
            "scope": self.scope,
            "patient": self.patient,
            "encounter": self.encounter,
            "need_patient_banner": self.need_patient_banner,
            "id_token": self.id_token,
            "fhir_user": self.fhir_user,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TokenSet:
        banner = data.get("need_patient_banner")
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=_optional_str(data.get("refresh_token")),
            expires_at=datetime.fromisoformat(str(data["expires_at"])),
            scope=str(data.get("scope") or ""),
            patient=_optional_str(data.get("patient")),
            encounter=_optional_str(data.get("encounter")),
            need_patient_banner=banner if isinstance(banner, bool) else None,
            id_token=_optional_str(data.get("id_token")),
            fhir_user=_optional_str(data.get("fhir_user")),
        )


class RefreshingToken:
    """Hands out an access token, refreshing 60 seconds early under one lock."""

    def __init__(
        self,
        smart: Any,
        tokens: TokenSet,
        on_refresh: Callable[[TokenSet], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._smart = smart
        self._tokens = tokens
        self._on_refresh = on_refresh
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        if not self._expiring():
            return self._tokens.access_token
        async with self._lock:
            if not self._expiring():
                return self._tokens.access_token
            await self._refresh_locked()
        return self._tokens.access_token

    async def refresh(self) -> str:
        async with self._lock:
            await self._refresh_locked()
        return self._tokens.access_token

    def _expiring(self) -> bool:
        return self._tokens.expires_at <= self._clock() + timedelta(seconds=60)

    async def _refresh_locked(self) -> None:
        if not self._tokens.refresh_token:
            raise AuthExpired()
        refreshed = await self._smart.exchange_refresh(
            self._tokens.refresh_token,
            self._tokens.scope,
        )
        self._tokens = refreshed
        if self._on_refresh is not None:
            await self._on_refresh(refreshed)


def fhir_user_from_id_token(id_token: str | None) -> str | None:
    """Read the fhirUser claim without verifying the signature."""
    if not id_token or id_token.count(".") < 2:
        return None
    payload_b64 = id_token.split(".")[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    fhir_user = payload.get("fhirUser")
    return fhir_user if isinstance(fhir_user, str) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
