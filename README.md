# aiofhir

Async FHIR R4 and SMART on FHIR client.

## Server-side token mode

Agents should not hold FHIR tokens. The optional `aiofhir[server]` backend holds
each signed-in user's tokens, encrypted, and agents read the record through
`RemoteRecord`, which has the same helpers as `PatientRecord`:

```python
from aiofhir import AuthExpired, RemoteRecord

async with RemoteRecord(BACKEND_URL, subject=user_id, token=SERVICE_TOKEN) as record:
    meds = await record.active_medications()  # a Summary, with citations
```

The subject is the application's id for the user (in LiveKit Agents 2.0, the
thread's `scope`). Your app backend starts sign-in with `POST /auth/start`
(`{"subject": ...}`, service bearer) and sends the user to the returned
`authorize_url`; the backend's `/auth/callback` stores the tokens under that
subject. An unknown or expired subject raises `AuthExpired`.

Run it with `just serve` after setting the variables listed in
`src/aiofhir/server/__main__.py`. Run it beside your app backend, not on the
agent worker. It keeps tokens in memory by default; pass
`EncryptedStore(key, backend)` a persistent mapping to keep them across restarts.

## Haste Health

The seed and smoke scripts for Haste Health live in [`haste/`](haste/README.md). They are not part of the package.

## Logging

The library only attaches a `NullHandler` to the `aiofhir` logger. Scripts call
`aiofhir.log.configure_cli_logging()`, which prints lines like:

```
2026-11-25T14:58:03.412-05:00 INFO  aiofhir.smart.discovery: discovered SMART configuration for https://...
```

`LOG_LEVEL` sets the level (default `INFO`) and `LOG_UTC=1` prints UTC timestamps.
Every line passes a redaction filter: bearer tokens, token JSON fields, and
`Patient/`, `User/`, `Membership/` ids are replaced, and request URLs hide
person-identifying search values.
