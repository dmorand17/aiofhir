# Examples

Small scripts that use aiofhir against a real SMART on FHIR server. Each one
reads its settings from environment variables and prints what it did. Tokens
are never printed.

| Script | Shows |
| --- | --- |
| [`discover.py`](discover.py) | Discovery: the SMART endpoints, the CapabilityStatement's resource types, and the scope aiofhir would request |
| [`standalone_launch.py`](standalone_launch.py) | A standalone patient sign-in from the command line (PKCE, `state`, a localhost callback), then a count of the patient's resources by type |
| [`saved_session.py`](saved_session.py) | Resuming a saved session: refresh, saving rotated tokens, and `AuthExpired` |
| [`backend_services.py`](backend_services.py) | SMART Backend Services: a `client_credentials` token with a client secret or `private_key_jwt`, and a system-level search |
| [`streamlit_app.py`](streamlit_app.py) | A browser app with sign-in, every patient-searchable type, and an attempt to open another patient |

Run them from the repo root after `just sync`, or anywhere `aiofhir` is installed.

## Register a client first

Register a SMART app on the server:

- For `standalone_launch.py` and `saved_session.py`: a patient-facing app with
  the `authorization_code` grant (and `refresh_token` for `saved_session.py`)
  and the redirect URI `http://localhost:8765/callback`, or set `REDIRECT_URI`
  to another localhost URL you registered.
- For `backend_services.py`: a confidential backend client with the
  `client_credentials` grant, and a client secret or a registered public key.

## Sign in, then resume

```bash
export BASE=https://fhir.example.org/r4 CLIENT_ID=my-app   # CLIENT_SECRET too, if confidential
TOKENS_FILE=tokens.json python examples/standalone_launch.py
TOKENS_FILE=tokens.json python examples/saved_session.py
```

`standalone_launch.py` opens your browser and waits up to five minutes for the
redirect. Sign in as a patient user: it needs a token with a `patient`. With
`TOKENS_FILE` set, it saves the tokens to that file, readable only by you.
`saved_session.py` reuses them, refreshing when the access token is due and
writing each new token set back to the file. Delete the file when you are done.

## Backend services

```bash
BASE=https://fhir.example.org/r4 CLIENT_ID=my-job CLIENT_SECRET=... \
  python examples/backend_services.py

BASE=... CLIENT_ID=my-job PRIVATE_KEY_FILE=key.pem KEY_ID=my-key-1 \
  python examples/backend_services.py              # needs aiofhir[jwt]
```

By default the scope is the `system/` read scopes the server advertises. If the
server lists only broader ones (Haste lists `system/*.cruds`), set `SCOPE` to
one it allows. `RESOURCE` picks the type to search (default `Patient`).

## Logging

The scripts call `aiofhir.log.configure_cli_logging()`: redacted log lines with
ISO 8601 timestamps. Set `LOG_LEVEL=DEBUG` to see each request.
