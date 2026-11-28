# Getting started

This guide signs a patient in with a standalone SMART launch and reads their
medications. It runs in one process, which suits a local script or demo. For an
agent in production, keep tokens in a backend instead; see
[server-side token mode](server.md).

## 1. Register an app

Register a SMART app on the FHIR server with:

- a redirect URI your app serves, for example `http://localhost:8765/callback`;
- the `authorization_code` grant, and `refresh_token` if you want long sessions;
- the scopes the server offers. aiofhir derives the request from the server's
  `.well-known/smart-configuration`; see [scopes](profiles.md#scopes).

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
    meds = await record.active_medications()
    upcoming = await record.upcoming_appointments(days=30)

print(meds.to_text())
```

`RefreshingToken` refreshes the access token 60 seconds before it expires. Each
helper returns a `Summary`; see [record helpers](helpers.md).

## 5. Show it to a person

`to_text()` is for a model: ISO dates in UTC and `Type/id` citations. For a
person, format the same summary in their time zone:

```python
from aiofhir.display import format_summary

print(format_summary(upcoming, tz="America/New_York"))
```

```text
upcoming_appointments
- «Annual checkup» Sat Nov 28, 2026, 10:07 PM EST (Appointment/a1)
```

## Try it in Streamlit

`examples/streamlit_app.py` runs this whole flow in a browser: sign-in, then
every helper, each shown formatted for a person and as `to_text()` for a model.

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
in that session's memory and are never shown. It is a local demo; agents should
use [server-side token mode](server.md).

## Logging

The library attaches only a `NullHandler` to the `aiofhir` logger. Scripts can
call `aiofhir.log.configure_cli_logging()`, which installs one handler with ISO
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
