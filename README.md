# aiofhir

An async FHIR R4 and SMART on FHIR client for Python, built on httpx. It
discovers a server's SMART configuration and CapabilityStatement, signs a user in
with PKCE, keeps the access token fresh, and searches the signed-in patient's
record for any resource type the server supports.

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
    print(sorted(await record.searchable_types()))  # from the CapabilityStatement
    meds = await record.search_raw("MedicationRequest", {"status": "active"})
    for resource in meds.resources:
        print(resource["id"])
```

The scope is built from the server's SMART configuration (and its
CapabilityStatement when no wildcard scope is offered), the resource types come
from the CapabilityStatement, and the patient id comes only from the token
response.

## 📦 Installation

```bash
pip install aiofhir                 # core: httpx only
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
uv add git+https://github.com/dmorand17/aiofhir --tag v0.1.0 --extra jwt      # with an extra
```

Or as a requirement string, for example in `pyproject.toml` or with pip:

```bash
uv add "aiofhir[jwt] @ git+https://github.com/dmorand17/aiofhir@v0.1.0"
pip install "aiofhir[jwt] @ git+https://github.com/dmorand17/aiofhir@v0.1.0"
```

`git+ssh://git@github.com/dmorand17/aiofhir` works the same way with an SSH key.

## 📖 Usage

| Guide | Covers |
| --- | --- |
| [Getting started](https://github.com/dmorand17/aiofhir/blob/main/docs/getting-started.md) | A first sign-in and patient-scoped search, step by step |
| [Sign-in and state handling](https://github.com/dmorand17/aiofhir/blob/main/docs/sign-in.md) | Standalone and EHR launch, state and PKCE, tokens and refresh, backend services |
| [Search and the FHIR client](https://github.com/dmorand17/aiofhir/blob/main/docs/search.md) | Patient-scoped search, the FHIR client, and the CapabilityStatement helpers |
| [Server profiles](https://github.com/dmorand17/aiofhir/blob/main/docs/profiles.md) | Haste Health, Epic, Oracle Health (Cerner) and generic servers; how scopes are chosen |

Epic and Oracle Health (Cerner) profiles are not yet verified against their
sandboxes. Haste Health is verified live; see [`haste/`](https://github.com/dmorand17/aiofhir/tree/main/haste)
for the seed and smoke scripts.

## 🔒 Security model

- The core library never stores tokens, and tokens never appear in `repr()`.
- Its own log lines and `FhirError` messages are redacted: tokens, person
  references and person-identifying search values are replaced. The raw
  OperationOutcome is on `FhirError.outcome`; it can hold PHI, so do not log it.
- For your own handlers, add `aiofhir.log.RedactingFilter()` so messages and
  tracebacks are redacted too, and keep `httpx` at `WARNING` (it logs full URLs).
  Redaction is best effort: a bare id with no context cannot be recognized, so
  still treat logs as sensitive.
- `PatientRecord.search_raw` adds `patient=` from the token itself, refuses a
  caller-supplied patient, and searches only types the CapabilityStatement says
  can be searched by patient.
- Storing OAuth `state` and the PKCE verifier, and using them once, is the
  caller's job; `verify_state()` compares in constant time.

## 🛠️ Development

You need [uv](https://docs.astral.sh/uv/) and [just](https://just.systems/).

```bash
git clone https://github.com/dmorand17/aiofhir && cd aiofhir
just sync       # install the project, all extras and the dev tools from uv.lock
just check      # what CI runs: sync, lint, format check, mypy strict, pytest
```

Run `just` with no arguments to list every recipe.

| Recipe | What it does |
| --- | --- |
| `just test [args]` | pytest; arguments pass through, for example `just test tests/test_capability.py -k scope` |
| `just lint` / `just fmt-check` | ruff lint / check formatting without writing files |
| `just fmt` / `just fix` | format with ruff / apply ruff autofixes and format |
| `just typecheck` | mypy strict over `src`, `tests`, `examples` and `haste` |
| `just lock` | refresh `uv.lock` after changing dependencies |
| `just discover` | print a server's SMART endpoints, its resource types and the scope aiofhir would request, for `BASE` |
| `just demo` | a Streamlit app that signs in and searches each supported type; see [getting started](https://github.com/dmorand17/aiofhir/blob/main/docs/getting-started.md#try-it-in-streamlit) |

### Live tests against Haste Health

Unit tests use recorded fixtures and need no network. The live tests are opt-in
and run against a seeded Haste project; [`haste/README.md`](https://github.com/dmorand17/aiofhir/blob/main/haste/README.md)
covers setup.

| Recipe | What it does |
| --- | --- |
| `just seed` | create or update the seed patient, chart, smoke client and access policy (`SEED_ALLOW_SEARCH=1` for searches) |
| `just smoke` | sign in as the patient in a browser and search each supported type; `SAVE_TOKENS=1` saves the token for the live tests |
| `AIOFHIR_LIVE=1 just test tests/live` | the live checks, using the saved token |
| `just check-policy` / `just check-policy-as-patient` | check the seed's access policy, as the admin or as the patient |
| `just seed-cleanup` | delete the admin seed client |

### Releasing

1. Add the changes under a new version in `CHANGELOG.md`, and set the same
   version in `pyproject.toml` (the only place it is defined).
2. Merge to `main` and let CI pass.
3. Publish a GitHub release tagged `v<version>`. The release workflow checks that
   the tag matches `pyproject.toml`, reruns the checks, and uploads to PyPI with
   trusted publishing.

## 📄 License

MIT. See [LICENSE](https://github.com/dmorand17/aiofhir/blob/main/LICENSE).
