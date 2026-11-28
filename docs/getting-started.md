# Getting started

This guide signs a patient in with a standalone SMART launch and searches their
record. It runs in one process, which suits a local script or demo. In
production, keep tokens in your backend.

## 1. Register an app

Register a SMART app on the FHIR server with:

- a redirect URI your app serves, for example `http://localhost:8765/callback`;
- the `authorization_code` grant, and `refresh_token` if you want long sessions;
- the scopes the server offers. aiofhir derives the request from the server's
  `.well-known/smart-configuration` and, when that lists no wildcard scope, its
  CapabilityStatement; see [scopes](profiles.md#scopes).

## 2. Start sign-in

```python
from aiofhir.smart import SmartClient

smart = SmartClient(
    "https://fhir.example.org/r4",  # the FHIR base URL
    client_id="my-app",
    redirect_uri="http://localhost:8765/callback",
)
request = await smart.authorization_request()
```

`authorization_request()` discovers the server, builds the scope, and creates a
256-bit `state` and a PKCE verifier (S256). Store `request.state` and
`request.code_verifier` on the server side, then send the user to `request.url`.

## 3. Handle the callback

```python
from aiofhir.smart import verify_state

verify_state(expected=saved_state, received=params["state"])  # raises StateMismatch
tokens = await smart.exchange_code(params["code"], saved_verifier)
tokens.patient  # the patient id, from the token response
```

Delete the saved state and verifier after this call, so they are used once.
See [sign-in and state handling](sign-in.md) for the rules.

## 4. Read the record

```python
from aiofhir import FhirClient, PatientRecord
from aiofhir.smart import RefreshingToken

holder = RefreshingToken(smart, tokens)
async with FhirClient(smart.base, holder, profile=smart.profile) as fhir:
    record = PatientRecord(fhir, patient_id=tokens.patient)
    print(sorted(await record.searchable_types()))
    meds = await record.search_raw("MedicationRequest", {"status": "active"})

for resource in meds.resources:
    print(resource["id"], resource.get("status"))
```

`RefreshingToken` refreshes the access token 60 seconds before it expires. The
types come from the server's CapabilityStatement; see
[search and the FHIR client](search.md) for more search recipes.

## Where tokens live

aiofhir never stores tokens: not on disk, not in a cache, not between runs. The
`TokenSet` from `exchange_code()` lives only in your process until you save it.
To keep a session across restarts, save it yourself, and save every refreshed
set too, because servers may rotate the refresh token and the old one stops
working:

```python
from aiofhir.smart import RefreshingToken, TokenSet


async def save(tokens: TokenSet) -> None:
    await my_store.put(user_id, encrypt(tokens.to_dict()))  # your storage, encrypted at rest


await save(tokens)  # right after exchange_code()
holder = RefreshingToken(smart, tokens, on_refresh=save)

# Later, in another process:
tokens = TokenSet.from_dict(decrypt(await my_store.get(user_id)))
holder = RefreshingToken(smart, tokens, on_refresh=save)
```

`repr(tokens)` shows only the patient and expiry, and aiofhir's logs and errors
never include a token. Keep tokens on your server, never in the browser. See
[`examples/saved_session.py`](../examples/saved_session.py) for a runnable
version that saves to a file.

## All together

The same flow as one script. A real app splits it in two: steps 2 and 3 run in
the request that starts sign-in and in the callback handler, with `state` and
the verifier stored server-side in between.

```python
import asyncio
from urllib.parse import parse_qsl, urlsplit

from aiofhir import FhirClient, PatientRecord
from aiofhir.smart import RefreshingToken, SmartClient, verify_state

BASE = "https://fhir.example.org/r4"


async def main() -> None:
    smart = SmartClient(BASE, client_id="my-app", redirect_uri="http://localhost:8765/callback")

    request = await smart.authorization_request()
    print("Sign in at:", request.url)
    callback = input("Paste the URL you were sent back to: ")
    params = dict(parse_qsl(urlsplit(callback).query))

    verify_state(expected=request.state, received=params["state"])
    tokens = await smart.exchange_code(params["code"], request.code_verifier)

    async with FhirClient(BASE, RefreshingToken(smart, tokens), profile=smart.profile) as fhir:
        record = PatientRecord(fhir, patient_id=tokens.patient)
        for resource in sorted(await record.searchable_types()):
            result = await record.search_raw(resource)
            print(f"{resource}: {len(result.resources)}")


asyncio.run(main())
```

Pasting the redirect URL by hand keeps the script short. If nothing listens on
the redirect URI, the browser shows an error page there, but the URL in the
address bar still has `code` and `state`. For a version that catches the
redirect itself, see
[`examples/standalone_launch.py`](../examples/standalone_launch.py).

## When things fail

Every aiofhir error subclasses `aiofhir.errors.AiofhirError`. The ones an app
usually handles:

```python
from aiofhir import AuthExpired
from aiofhir.errors import FhirError, StateMismatch, TokenError

try:
    result = await record.search_raw("Observation", {"category": "laboratory"})
except AuthExpired as exc:
    ...  # the refresh token is gone or revoked: send the user to sign in again
except FhirError as exc:
    print(exc.status, exc)  # the message is redacted; exc.outcome may hold PHI
except ValueError as exc:
    ...  # search_raw refused the type or a parameter (see search.md)
```

| Error | Raised when |
| --- | --- |
| `DiscoveryError` | the SMART configuration or CapabilityStatement is missing or unusable, or PKCE S256 is not supported |
| `StateMismatch` | `verify_state()` got a different `state`: drop the callback |
| `TokenError` | the token endpoint refused the request; the message says why |
| `AuthExpired` | the refresh token was refused or is missing; its message is safe to show the user |
| `FhirError` | a read or search failed after retries; `status` is the HTTP status |

## Try it in Streamlit

`examples/streamlit_app.py` runs this whole flow in a browser: sign-in, then a
patient-scoped search of every type the server's CapabilityStatement supports.

```bash
just demo        # http://localhost:8765, local only
```

It reads `BASE`, `SMOKE_CLIENT_ID` and `SMOKE_CLIENT_SECRET` from the repo-root
`.env`, and needs a SMART client whose redirect URIs include
`http://localhost:8765/callback` (on Haste, `just seed` creates it). Set
`DEMO_REDIRECT_URI` for another redirect, and `AIOFHIR_PROFILE` to pick a
profile. Streamlit comes from the `demo` dependency group, which `just demo`
installs.

The app keeps `state` and the verifier in a server-side store keyed by `state`,
because the browser returns from sign-in in a new Streamlit session. Tokens stay
in that session's memory and are never shown. It is a local demo; in production,
keep tokens in your backend.

## More examples

[`examples/`](../examples) has runnable scripts: a command-line sign-in, resuming
a saved session with refresh, SMART Backend Services, and discovery.

## Logging

Set `AIOFHIR_LOG_LEVEL` (for example `DEBUG`) to see aiofhir's logs without
writing any logging code. It is read on `import aiofhir` and changes only the
`aiofhir` logger:

- In an app that configures logging, aiofhir's records go
  to the app's handlers, in its format, at that level. Nothing else changes.
- In a script with no logging set up, aiofhir prints them to stderr itself, with
  ISO 8601 timestamps and the redaction filter below, until the app configures
  logging of its own.
- Unset, the library only has a `NullHandler`: nothing prints.

```bash
AIOFHIR_LOG_LEVEL=DEBUG python my_app.py
```

Scripts can instead
call `aiofhir.log.configure_cli_logging()`, which installs one root handler with ISO
8601 timestamps and a redaction filter: bearer tokens, token fields, and
`Patient/`, `User/` and `Membership/` ids are replaced, and request URLs hide
person-identifying search values. `LOG_LEVEL` sets the level (default `INFO`)
and `LOG_UTC=1` prints UTC times. It also keeps `httpx` at `WARNING`, because
httpx logs full URLs.

In an application with its own logging setup, add the filter to each handler
that writes text. It redacts the message, the traceback and `stack_info`:

```python
import logging

from aiofhir.log import RedactingFilter

handler = logging.StreamHandler()
handler.addFilter(RedactingFilter())
logging.getLogger().addHandler(handler)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
```

`FhirError` messages are redacted the same way. The server's raw
OperationOutcome is on `FhirError.outcome`; it can hold PHI, so do not log it.
Redaction is best effort (a bare id with no context cannot be recognized), so
keep treating logs as sensitive.
