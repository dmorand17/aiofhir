# AGENTS.md

`aiofhir` is a thin async FHIR R4 and SMART on FHIR client. Add changes to `CHANGELOG.md`; a release is a GitHub release whose tag matches the version in `pyproject.toml`. Epic and Cerner profiles wait for sandbox access.

## Commands

- Run `just check` before finishing. It syncs from `uv.lock`, then runs ruff, the format check, mypy, and pytest.
- `just test` runs pytest. Extra arguments are passed through.
- Supported Python is 3.11 through 3.13. mypy is strict.

## Layout

- `src/aiofhir/smart/`: discovery, scopes, the authorization-code flow, tokens and refresh.
- `src/aiofhir/capability.py`: reads the CapabilityStatement.
- `src/aiofhir/client.py`: `FhirClient` (read, search, paging, retries).
- `src/aiofhir/record.py`: `PatientRecord.search_raw`, scoped to the token's patient.
- `src/aiofhir/profiles/`: per-server quirks.
- `haste/`: seed, smoke and policy tooling for the live Haste project. Not part of the package.

## Library rules

- The core depends on httpx only. Importing `aiofhir` must not load optional extras such as `jwt`.
- Resource types are never hardcoded. They come from the server's CapabilityStatement (`aiofhir.capability`).
- The patient id comes only from the token response. `PatientRecord.search_raw` adds `patient=` itself and does not accept it as an argument.
- Scopes come from the server's SMART `capabilities` and `scopes_supported`, plus the CapabilityStatement's types when no wildcard scope is listed. Profiles adjust discovery, search and paging. They do not choose scopes or list resource types.
- An explicit `scope=` is only for an app registration that allows less than the server advertises.
- The core library does not store tokens. Tokens never appear in `repr()`, logs, or tracebacks. Patient ids in query strings are redacted in hooks.
- `verify_state()` uses `hmac.compare_digest`. Storing `state` and `code_verifier`, and using them once, is the caller's job.

## Docs

When a change touches the public API, a command or a behavior described in `README.md` or `docs/`, update them in the same change. Keep the docs about FHIR and SMART on FHIR; they name no specific consumer of the library.

## Out of scope

Writes, typed models for resources, FHIR versions other than R4, bulk data, and token storage in the core stay out unless a change asks for them.
