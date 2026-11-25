"""Browser sign-in and a printed record for the Haste smoke script."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import webbrowser
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from aiofhir import FhirClient, PatientRecord
from aiofhir.display import format_summary
from aiofhir.errors import TokenError
from aiofhir.profiles import HasteProfile
from aiofhir.scrub import scrub
from aiofhir.smart import RefreshingToken, SmartClient, TokenSet, verify_state
from aiofhir.smart.client import AuthorizationRequest

REDIRECT_URI = "http://localhost:8765/callback"
# Admin sign-in for seeding. No offline_access: the token should not outlive the run.
SEED_LOGIN_SCOPE = "user/*.cruds openid fhirUser"
logger = logging.getLogger(__name__)
Browser = Callable[[str], Awaitable[None] | None]
_HELPERS = (
    "upcoming_appointments",
    "active_medications",
    "allergies",
    "conditions",
    "recent_labs",
)


CALLBACK_TIMEOUT = 300.0
_NO_PATIENT = (
    "Sign in as the seeded patient user (SEED_USER_EMAIL or SEED_USER_ID), whose Membership "
    "links to the seed Patient, not as the admin. That user needs the `member` role: Haste "
    "drops patient scopes for an `owner`. Use a private window so the admin session is not reused."
)


class SignInError(RuntimeError):
    """The browser sign-in did not return a usable code."""


async def capture_callback(
    expected_state: str,
    port: int = 8765,
    host: str | None = None,
) -> tuple[asyncio.Server, asyncio.Future[str]]:
    """Listen for the OAuth redirect and check its state.

    The redirect URI says `localhost`, which browsers may resolve to ::1, so
    both loopback addresses are bound unless `host` is given.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(_read_callback(reader, writer, expected_state, future))
        task.add_done_callback(_discard)

    if host is not None:
        server = await asyncio.start_server(handler, host, port)
    else:
        try:
            server = await asyncio.start_server(handler, ["127.0.0.1", "::1"], port)
        except OSError:
            server = await asyncio.start_server(handler, "127.0.0.1", port)
    return server, future


async def wait_for_code(future: asyncio.Future[str], timeout: float = CALLBACK_TIMEOUT) -> str:
    try:
        return await asyncio.wait_for(future, timeout)
    except TimeoutError as exc:
        raise SignInError(f"no sign-in callback within {int(timeout)} seconds") from exc


async def run_smoke(
    base: str,
    client_id: str,
    client_secret: str | None,
    code: str,
    verifier: str,
    on_tokens: Callable[[TokenSet], Awaitable[None]] | None = None,
) -> str:
    """Exchange a code, print every helper, and force one refresh.

    ``on_tokens`` receives the exchanged tokens and each rotated set after a refresh.
    """
    smart = SmartClient(
        base,
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
        client_secret=client_secret,
        profile=HasteProfile(),
    )
    try:
        tokens = await smart.exchange_code(code, verifier)
    except TokenError as exc:
        raise SignInError(f"{exc}. {_NO_PATIENT}") from exc
    if not tokens.patient:
        raise SignInError(f"Token response did not include a patient. {_NO_PATIENT}")
    if on_tokens is not None:
        await on_tokens(tokens)
    holder = RefreshingToken(smart, tokens, on_refresh=on_tokens)
    async with FhirClient(base, holder, profile=HasteProfile()) as fhir:
        record = PatientRecord(fhir, patient_id=tokens.patient)
        sections = [f"patient: {tokens.patient}"]
        for name in _HELPERS:
            summary = await getattr(record, name)()
            sections.append(format_summary(summary, tz="America/New_York"))
        logger.info("forcing one token refresh")
        await holder.refresh()
        if holder.tokens.patient != tokens.patient:
            raise SignInError("the refreshed token lost the patient context")
        sections.append("refresh: ok, patient kept")
    report = scrub("\n\n".join(sections))
    logger.info("record helpers printed")
    _reject_token(report, tokens.access_token)
    if tokens.refresh_token:
        _reject_token(report, tokens.refresh_token)
    return report


def _open_browser(url: str) -> None:
    webbrowser.open(url)


def save_env_value(path: Path, name: str, value: str) -> None:
    """Set one env assignment. The value is written to the file and not logged."""
    lines = path.read_text().splitlines() if path.exists() else []
    prefix = f"{name}="
    rewritten = [prefix + value if line.startswith(prefix) else line for line in lines]
    if not any(line.startswith(prefix) for line in lines):
        rewritten.append(prefix + value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(rewritten) + "\n")


def save_tokens(path: Path, tokens: TokenSet) -> None:
    """Write a TokenSet for the live tests. The file is owner-only and never logged."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(tokens.to_dict(), handle)


def load_tokens(path: Path) -> TokenSet:
    return TokenSet.from_dict(json.loads(path.read_text()))


async def login_access_token(
    base: str,
    client_id: str,
    client_secret: str | None,
    *,
    scope: str,
    open_browser: Browser | None = None,
    port: int = 8765,
) -> str:
    """Sign in with an existing client and return the access token."""
    logger.info("signing in with client %s", client_id)
    logger.debug("login scope %s", scope)
    smart = SmartClient(
        base,
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
        client_secret=client_secret,
        profile=HasteProfile(),
        scope=scope,
    )
    request = await smart.authorization_request()
    server, future = await capture_callback(request.state, port=port)
    browser = open_browser or _open_browser
    async with server:
        opened = browser(request.url)
        if inspect.isawaitable(opened):
            await opened
        code = await wait_for_code(future)
    tokens = await smart.exchange_code(code, request.code_verifier)
    return tokens.access_token


async def authorization(
    base: str,
    client_id: str,
    client_secret: str | None,
) -> AuthorizationRequest:
    smart = SmartClient(
        base,
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
        client_secret=client_secret,
        profile=HasteProfile(),
    )
    return await smart.authorization_request()


async def _read_callback(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    expected_state: str,
    future: asyncio.Future[str],
) -> None:
    status = b"400 Bad Request"
    body = b"Sign-in failed."
    raw = await reader.read(8192)
    parts = raw.decode("latin1").split(" ", 2)
    path = parts[1] if len(parts) > 1 else ""
    if urlsplit(path).path != "/callback":
        # Preconnects, favicon requests and empty sockets are not the redirect.
        if raw:
            await _respond(writer, b"404 Not Found", b"Not found.")
        else:
            writer.close()
        return
    try:
        query = parse_qs(urlsplit(path).query)
        verify_state(expected=expected_state, received=query.get("state", [""])[0])
        error = query.get("error", [""])[0]
        if error:
            detail = query.get("error_description", [""])[0]
            raise SignInError(f"authorization failed: {error} {detail}".strip())
        code = query.get("code", [""])[0]
        if not code:
            raise ValueError("missing code")
        if not future.done():
            logger.info("received authorization callback")
            future.set_result(code)
        status = b"200 OK"
        body = b"Signed in. You can close this tab."
    except Exception as exc:
        if not future.done():
            future.set_exception(exc)
    await _respond(writer, status, body)


async def _respond(writer: asyncio.StreamWriter, status: bytes, body: bytes) -> None:
    writer.write(
        b"HTTP/1.1 "
        + status
        + b"\r\nContent-Length: "
        + str(len(body)).encode("ascii")
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )
    await writer.drain()
    writer.close()


def _discard(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    task.exception()


def _reject_token(report: str, token: str) -> None:
    if token and token in report:
        raise RuntimeError("smoke report included a token")
