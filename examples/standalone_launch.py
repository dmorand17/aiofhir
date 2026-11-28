"""Sign a patient in from the command line, then count their records by type.

A standalone SMART launch: the script opens the browser at the server's sign-in
page, catches the redirect on localhost, checks `state`, and exchanges the code
(PKCE S256). It then searches each type the server's CapabilityStatement can
scope to a patient and prints how many resources each one returned.

Environment:
- BASE: the FHIR base URL.
- CLIENT_ID, and CLIENT_SECRET for a confidential client.
- REDIRECT_URI (default http://localhost:8765/callback): must be registered for
  the client and point at localhost.
- TOKENS_FILE (optional): save the tokens here (mode 600), for saved_session.py.

    BASE=https://fhir.example.org/r4 CLIENT_ID=my-app python examples/standalone_launch.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from aiofhir import FhirClient, PatientRecord
from aiofhir.log import configure_cli_logging
from aiofhir.smart import RefreshingToken, SmartClient, TokenSet, verify_state

DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"


async def sign_in(
    smart: SmartClient,
    open_url: Callable[[str], object] = webbrowser.open,
    timeout: float = 300.0,
) -> TokenSet:
    """Run the authorization-code flow and return the tokens."""
    request = await smart.authorization_request()
    # state and the verifier stay in this process and are used once.
    params = await _wait_for_callback(smart.redirect_uri, lambda: open_url(request.url), timeout)
    if "error" in params:
        raise RuntimeError(
            f"sign-in failed: {params['error']} {params.get('error_description', '')}"
        )
    verify_state(expected=request.state, received=params.get("state", ""))
    return await smart.exchange_code(params.get("code", ""), request.code_verifier)


async def count_by_type(smart: SmartClient, tokens: TokenSet) -> dict[str, tuple[int, bool]]:
    """For each patient-searchable type: (resources returned, whether paging stopped early)."""
    if tokens.patient is None:
        raise RuntimeError("the token has no patient; sign in as a patient user")
    holder = RefreshingToken(smart, tokens)
    async with FhirClient(smart.base, holder, profile=smart.profile) as fhir:
        record = PatientRecord(fhir, patient_id=tokens.patient)
        counts: dict[str, tuple[int, bool]] = {}
        for resource in sorted(await record.searchable_types()):
            result = await record.search_raw(resource)
            counts[resource] = (len(result.resources), result.truncated)
    return counts


def save_tokens(path: Path, tokens: TokenSet) -> None:
    """Write the tokens readable by this user only. Encrypt them at rest in a real app."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(tokens.to_dict(), handle)


async def _wait_for_callback(
    redirect_uri: str, start: Callable[[], object], timeout: float
) -> dict[str, str]:
    """Serve the redirect URI on localhost until the browser comes back to it."""
    target = urlsplit(redirect_uri)
    if target.hostname not in {"localhost", "127.0.0.1", "::1"} or target.port is None:
        raise ValueError("REDIRECT_URI must be http://localhost:<port>/<path>")
    received: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = (await reader.readline()).decode("latin1").split(" ")
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass  # skip the headers
        url = urlsplit(request_line[1] if len(request_line) > 2 else "")
        if url.path == target.path and not received.done():
            received.set_result(dict(parse_qsl(url.query)))
            status, body = b"200 OK", b"Signed in. You can close this tab."
        else:
            status, body = b"404 Not Found", b"Not found."  # favicon and the like
        writer.write(
            b"HTTP/1.1 " + status + b"\r\nContent-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, target.hostname, target.port)
    async with server:
        start()
        return await asyncio.wait_for(received, timeout)


async def main() -> None:
    configure_cli_logging()
    base, client_id = os.environ.get("BASE", ""), os.environ.get("CLIENT_ID", "")
    if not base or not client_id:
        sys.exit("Set BASE and CLIENT_ID.")
    smart = SmartClient(
        base,
        client_id=client_id,
        redirect_uri=os.environ.get("REDIRECT_URI") or DEFAULT_REDIRECT_URI,
        client_secret=os.environ.get("CLIENT_SECRET") or None,
    )

    def open_url(url: str) -> None:
        print(f"Opening the sign-in page. If no browser opens, visit:\n{url}\n")
        webbrowser.open(url)

    tokens = await sign_in(smart, open_url)
    if tokens_file := os.environ.get("TOKENS_FILE"):
        save_tokens(Path(tokens_file), tokens)
        print(f"Saved the tokens to {tokens_file}.")
    print(f"Signed in. Scope: {tokens.scope}\n")
    for resource, (count, truncated) in (await count_by_type(smart, tokens)).items():
        print(f"{resource}: {count}{'+' if truncated else ''}")


if __name__ == "__main__":
    asyncio.run(main())
