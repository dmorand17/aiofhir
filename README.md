# aiofhir

An async FHIR R4 and SMART on FHIR client for Python, built on httpx. It signs a
patient in, reads their record, and returns compact, cited summaries that an AI
agent can read aloud: upcoming appointments, medications, allergies, labs and
more. It is built for LiveKit voice and chat agents but has no LiveKit
dependency.

## 🚀 Quick start

```python
from aiofhir import FhirClient, PatientRecord
from aiofhir.smart import RefreshingToken, SmartClient, verify_state

smart = SmartClient(FHIR_BASE, client_id="my-app", redirect_uri="https://my.app/callback")

request = await smart.authorization_request()  # store request.state and request.code_verifier
# ... send the user to request.url; on the callback:
verify_state(expected=saved.state, received=callback_state)
tokens = await smart.exchange_code(code, saved.code_verifier)

async with FhirClient(FHIR_BASE, RefreshingToken(smart, tokens), profile=smart.profile) as fhir:
    record = PatientRecord(fhir, patient_id=tokens.patient)
    meds = await record.active_medications()
    print(meds.to_text())
```

```text
active_medications
- «Lisinopril» (MedicationRequest/mr-inline)
- «Metformin» (MedicationRequest/mr-remote)
```

The scope is built from the server's SMART configuration, the patient id comes
only from the token response, and every item cites the FHIR resource it came from.

## 📦 Installation

```bash
pip install aiofhir                 # core: httpx only
pip install "aiofhir[server]"       # reference backend for RemoteRecord (FastAPI)
pip install "aiofhir[jwt]"          # private_key_jwt client authentication (PyJWT)
```

Python 3.11 to 3.13.

### From GitHub

Install straight from the repository with uv. Pin a release tag for a
reproducible build; uv records the source under `[tool.uv.sources]` in your
`pyproject.toml`.

```bash
uv add git+https://github.com/dmorand17/aiofhir --tag v0.1.0                  # a release
uv add git+https://github.com/dmorand17/aiofhir --branch main                 # latest main
uv add git+https://github.com/dmorand17/aiofhir --tag v0.1.0 --extra server   # with an extra
```

Or as a requirement string, for example in `pyproject.toml` or with pip:

```bash
uv add "aiofhir[server] @ git+https://github.com/dmorand17/aiofhir@v0.1.0"
pip install "aiofhir[server] @ git+https://github.com/dmorand17/aiofhir@v0.1.0"
```

`git+ssh://git@github.com/dmorand17/aiofhir` works the same way with an SSH key.

## 📖 Usage

| Guide | Covers |
| --- | --- |
| [Getting started](https://github.com/dmorand17/aiofhir/blob/main/docs/getting-started.md) | A first sign-in and record read, step by step |
| [Sign-in and state handling](https://github.com/dmorand17/aiofhir/blob/main/docs/sign-in.md) | Standalone and EHR launch, state and PKCE, tokens and refresh, backend services |
| [Record helpers](https://github.com/dmorand17/aiofhir/blob/main/docs/helpers.md) | The ten helpers, `Summary`, raw search, and the FHIR client |
| [Server profiles](https://github.com/dmorand17/aiofhir/blob/main/docs/profiles.md) | Haste Health, Epic, Oracle Health (Cerner) and generic servers; how scopes are chosen |
| [Server-side token mode](https://github.com/dmorand17/aiofhir/blob/main/docs/server.md) | Keeping tokens out of agents with `RemoteRecord` and `aiofhir[server]` |

Epic and Oracle Health (Cerner) profiles are not yet verified against their
sandboxes. Haste Health is verified live; see [`haste/`](https://github.com/dmorand17/aiofhir/tree/main/haste)
for the seed and smoke scripts.

## 🔒 Security model

- The core library never stores tokens. Tokens never appear in `repr()` or logs.
- Helpers add `patient=` from the token themselves and refuse a caller-supplied patient.
- Agents should hold no FHIR tokens at all: use `RemoteRecord` with the
  `aiofhir[server]` backend.
- Storing OAuth `state` and the PKCE verifier, and using them once, is the
  caller's job; `verify_state()` compares in constant time.

## 🛠️ Development

```bash
just check      # sync from uv.lock, then ruff, format check, mypy strict, pytest
```

See [CHANGELOG.md](https://github.com/dmorand17/aiofhir/blob/main/CHANGELOG.md) for releases.

## 📄 License

MIT. See [LICENSE](https://github.com/dmorand17/aiofhir/blob/main/LICENSE).
