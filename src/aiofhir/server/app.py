"""Reference backend: holds each subject's tokens and serves the record helpers.

Run it beside the application's backend, never on the agent worker. Agents
reach it through `aiofhir.RemoteRecord`, which never sees a token.
"""

from __future__ import annotations

import hmac
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from aiofhir.client import FhirClient
from aiofhir.errors import AiofhirError, AuthExpired, FhirError
from aiofhir.log import redact_text
from aiofhir.profiles.base import ServerProfile
from aiofhir.record import RECORD_HELPERS, PatientRecord, Record
from aiofhir.server.store import EncryptedStore
from aiofhir.smart.client import SmartClient
from aiofhir.smart.tokens import RefreshingToken, TokenSet

logger = logging.getLogger(__name__)

# Helper name -> the arguments it accepts, from the Record protocol. Nothing else is reachable.
RECORD_METHODS: dict[str, frozenset[str]] = {
    name: frozenset(p for p in inspect.signature(getattr(Record, name)).parameters if p != "self")
    for name in RECORD_HELPERS
}
_MAX_DAYS = 365
_AUTH_EXPIRED = {"error": "auth_expired"}
# An OAuth error_description is free text; keep log lines short.
_MAX_ERROR_TEXT = 200


class SubjectBody(BaseModel):
    subject: str = Field(min_length=1, max_length=256)


class RecordBody(SubjectBody):
    args: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class _Pending:
    subject: str
    verifier: str
    expires: float


def create_app(
    *,
    fhir_base_url: str,
    client_id: str,
    redirect_uri: str,
    service_token: str,
    store: EncryptedStore,
    client_secret: str | None = None,
    scope: str | None = None,
    profile: ServerProfile | None = None,
    state_ttl: float = 600.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Build the ASGI app. `redirect_uri` must point at this app's `/auth/callback`."""
    if not service_token:
        raise ValueError("service_token must not be empty")
    smart = SmartClient(
        fhir_base_url,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        client_secret=client_secret,
        profile=profile,
    )
    pending: dict[str, _Pending] = {}
    # One RefreshingToken per subject, so concurrent requests share one refresh.
    holders: dict[str, RefreshingToken] = {}

    def require_service(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {service_token}".encode()
        if authorization is None or not hmac.compare_digest(authorization.encode(), expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    def forget(subject: str) -> None:
        holders.pop(subject, None)
        store.delete(subject)

    def holder_for(subject: str) -> RefreshingToken | None:
        holder = holders.get(subject)
        if holder is not None:
            return holder
        tokens = store.get(subject)
        if tokens is None or not tokens.patient:
            return None

        async def save(refreshed: TokenSet) -> None:
            store.put(subject, refreshed)

        return holders.setdefault(subject, RefreshingToken(smart, tokens, on_refresh=save))

    app = FastAPI(title="aiofhir record backend", docs_url=None, redoc_url=None)
    service = [Depends(require_service)]

    @app.post("/auth/start", dependencies=service)
    async def start(body: SubjectBody) -> dict[str, str]:
        now = monotonic()
        for state in [s for s, p in pending.items() if p.expires <= now]:
            del pending[state]
        request = await smart.authorization_request()
        pending[request.state] = _Pending(body.subject, request.code_verifier, now + state_ttl)
        logger.info("sign-in started")
        return {"authorize_url": request.url}

    @app.get("/auth/callback", response_class=HTMLResponse)
    async def callback(
        state: str = "", code: str = "", error: str = "", error_description: str = ""
    ) -> HTMLResponse:
        entry = pending.pop(state, None)  # single use, even when the rest fails
        if entry is None or entry.expires <= monotonic():
            logger.info("sign-in callback refused: unknown, used or expired state")
            return _page(400, "This sign-in link has expired or was already used.")
        if error:
            logger.info(
                "sign-in refused by the authorization server: %s",
                _oauth_text(error, error_description),
            )
            return _page(400, "Sign-in was not completed.")
        if not code:
            logger.info("sign-in callback had no authorization code")
            return _page(400, "Sign-in was not completed.")
        try:
            tokens = await smart.exchange_code(code, entry.verifier)
        except (AiofhirError, httpx.HTTPError) as exc:
            logger.info("code exchange failed: %s: %s", type(exc).__name__, exc)
            return _page(400, "Sign-in could not be completed.")
        if not tokens.patient:
            logger.info("sign-in did not select a patient record")
            return _page(400, "This sign-in did not select a patient record.")
        store.put(entry.subject, tokens)
        holders.pop(entry.subject, None)
        logger.info("sign-in completed")
        return _page(200, "Your health record is connected. You can close this tab.")

    @app.post("/auth/disconnect", dependencies=service, status_code=204)
    async def disconnect(body: SubjectBody) -> Response:
        forget(body.subject)
        return Response(status_code=204)

    @app.post("/v1/record/{method}", dependencies=service)
    async def record(method: str, body: RecordBody) -> JSONResponse:
        allowed = RECORD_METHODS.get(method)
        if allowed is None:
            raise HTTPException(status_code=404, detail="unknown method")
        args = _validated_args(body.args, allowed)
        holder = holder_for(body.subject)
        if holder is None:
            return JSONResponse(_AUTH_EXPIRED, status_code=401)
        patient = holder.tokens.patient
        assert patient is not None  # holder_for only builds holders with a patient
        try:
            async with FhirClient(fhir_base_url, holder, profile=smart.profile) as fhir:
                summary = await getattr(PatientRecord(fhir, patient_id=patient), method)(**args)
        except AuthExpired:
            forget(body.subject)
            return JSONResponse(_AUTH_EXPIRED, status_code=401)
        except FhirError as exc:
            logger.info("record request failed with status %s", exc.status)
            return JSONResponse({"error": "fhir_error", "status": exc.status}, status_code=502)
        return JSONResponse(summary.to_dict())

    return app


def _validated_args(args: dict[str, Any], allowed: frozenset[str]) -> dict[str, int]:
    unknown = set(args) - allowed
    if unknown:
        raise HTTPException(status_code=422, detail=f"unsupported arguments: {sorted(unknown)}")
    validated: dict[str, int] = {}
    if "days" in args:
        days = args["days"]
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= _MAX_DAYS:
            raise HTTPException(status_code=422, detail=f"days must be 1 to {_MAX_DAYS}")
        validated["days"] = days
    return validated


def _oauth_text(error: str, description: str) -> str:
    """An OAuth error from the callback URL, cut short and redacted for the log."""
    parts = [value[:_MAX_ERROR_TEXT] for value in (error, description) if value]
    return redact_text(": ".join(parts))


def _page(status: int, message: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><title>aiofhir</title><p>{message}</p>", status_code=status
    )
