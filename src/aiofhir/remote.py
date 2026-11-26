"""Read a patient record through the token-holding backend.

`RemoteRecord` has the same helpers as `PatientRecord` and returns the same
`Summary`, but it never sees a FHIR token. It names a subject (the signed-in
user); the backend maps that subject to its tokens and patient.
"""

from __future__ import annotations

import types
from typing import Any

import httpx

from aiofhir.errors import AuthExpired, FhirError
from aiofhir.summary import Summary


class RemoteRecord:
    """Patient-record helpers served by `aiofhir.server` for one subject."""

    def __init__(
        self,
        base_url: str,
        subject: str,
        *,
        token: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self.subject = subject
        self._token = token
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"RemoteRecord(base_url={self._base!r}, subject={self.subject!r})"

    async def __aenter__(self) -> RemoteRecord:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def upcoming_appointments(self, days: int = 90) -> Summary:
        return await self._call("upcoming_appointments", {"days": days})

    async def active_medications(self) -> Summary:
        return await self._call("active_medications", {})

    async def allergies(self) -> Summary:
        return await self._call("allergies", {})

    async def conditions(self) -> Summary:
        return await self._call("conditions", {})

    async def recent_labs(self, days: int = 90) -> Summary:
        return await self._call("recent_labs", {"days": days})

    async def immunizations(self) -> Summary:
        return await self._call("immunizations", {})

    async def vital_signs(self, days: int = 30) -> Summary:
        return await self._call("vital_signs", {"days": days})

    async def past_visits(self, days: int = 365) -> Summary:
        return await self._call("past_visits", {"days": days})

    async def procedures(self, days: int = 365) -> Summary:
        return await self._call("procedures", {"days": days})

    async def documents(self, days: int = 365) -> Summary:
        return await self._call("documents", {"days": days})

    async def _call(self, method: str, args: dict[str, Any]) -> Summary:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await self._client.post(
                f"{self._base}/v1/record/{method}",
                json={"subject": self.subject, "args": args},
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise FhirError(0, "record service request failed") from exc
        if response.status_code == 401 and _error(response) == "auth_expired":
            raise AuthExpired()
        if response.status_code != 200:
            raise FhirError(response.status_code, "record service refused the request")
        try:
            return Summary.from_dict(response.json())
        except ValueError as exc:
            raise FhirError(response.status_code, "record service sent an invalid summary") from exc


def _error(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    value = body.get("error") if isinstance(body, dict) else None
    return value if isinstance(value, str) else None
