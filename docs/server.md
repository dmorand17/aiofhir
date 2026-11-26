# Server-side token mode

An AI agent should not hold FHIR tokens: a prompt injection or a bug could leak
a long-lived refresh token to someone's medical record. The optional
`aiofhir[server]` backend holds each signed-in user's tokens, encrypted, and
agents read the record through `RemoteRecord`, which never sees a token.

```text
browser ──sign in──▶ SMART server
                        │ redirect with code
                        ▼
          aiofhir[server] backend  ── one encrypted TokenSet per subject
                        ▲
   agent ──RemoteRecord(subject).active_medications()──┘   (a Summary, never a token)
```

## RemoteRecord

```python
from aiofhir import AuthExpired, RemoteRecord

async with RemoteRecord(BACKEND_URL, subject=user_id, token=SERVICE_TOKEN) as record:
    meds = await record.active_medications()
```

- It has the same ten helpers as `PatientRecord` and returns the same `Summary`.
- The subject is the application's id for the signed-in user. In LiveKit Agents
  2.0 it is the thread's `scope`, which the app backend sets.
- An unknown or expired subject raises `AuthExpired`. A wrong service token
  raises `FhirError(401)`.
- `RemoteRecord` uses httpx only; it is part of the core package.

## The backend

| Route | Who calls it | What |
| --- | --- | --- |
| `POST /auth/start` | the app backend, with the service bearer | `{"subject": ...}` → `{"authorize_url": ...}` |
| `GET /auth/callback` | the user's browser | exchanges the code and stores the tokens under the subject |
| `POST /auth/disconnect` | the app backend | deletes the subject's tokens |
| `POST /v1/record/{method}` | agents, with the service bearer | runs one helper for `{"subject": ..., "args": {...}}` |

- `state` is single-use and expires after 10 minutes.
- Only the ten helper names are served, and the only argument accepted is `days`
  (1 to 365). A `patient` argument is refused.
- One refresher per subject, so concurrent requests share one refresh; refreshed
  tokens are saved back, and a dead refresh token deletes the subject.

### Run it

```bash
export AIOFHIR_FHIR_BASE=https://fhir.example.org/r4
export AIOFHIR_CLIENT_ID=my-app
export AIOFHIR_REDIRECT_URI=http://localhost:8000/auth/callback   # register this on the client
export AIOFHIR_SERVICE_TOKEN=$(openssl rand -hex 32)
export AIOFHIR_FERNET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
python -m aiofhir.server        # or: just serve
```

Optional: `AIOFHIR_CLIENT_SECRET`, `AIOFHIR_PROFILE` (`generic`, `haste`, `epic`,
`cerner`), `AIOFHIR_HOST` (default `127.0.0.1`), `AIOFHIR_PORT` (default `8000`).
Access logs are off, because the callback URL carries the authorization code
and `state`.

### Embed it

```python
from aiofhir.server import EncryptedStore, create_app

app = create_app(
    fhir_base_url=...,
    client_id=...,
    redirect_uri=...,
    service_token=...,
    store=EncryptedStore(fernet_key, backend=my_bytes_mapping),
)
```

`EncryptedStore(key, backend)` Fernet-encrypts each `TokenSet` and writes bytes
into any `MutableMapping[str, bytes]` (a dict by default, so tokens are lost on
restart). Keys are an HMAC of the subject, so the backing store never sees it.

## Deployment rules

- Run the backend beside your application's backend, **never on the agent
  worker**. LiveKit Agents 2.0 can serve HTTP routes from the worker
  (`server.http`); do not mount this backend there.
- Run one backend process. Pending sign-ins and the per-subject refresh lock are
  in memory, so several processes would break single-use state and token rotation.
- Keep the service token and Fernet key in a secret store.
