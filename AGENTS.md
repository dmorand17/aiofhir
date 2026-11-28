# AGENTS.md

`aiofhir` is a thin async FHIR R4 and SMART on FHIR client. Add changes to `CHANGELOG.md`; a release is a GitHub release whose tag matches the version in `pyproject.toml`. Epic and Cerner profiles wait for sandbox access.

## Commands

- Run `just check` before finishing. It syncs from `uv.lock`, then runs ruff, the format check, mypy, and pytest.
- `just test` runs pytest. Extra arguments are passed through.
- Supported Python is 3.11 through 3.13. mypy is strict.

## Library rules

- The core depends on httpx only. Importing `aiofhir` must not load optional extras such as `jwt`.
- Resource types are never hardcoded. They come from the server's CapabilityStatement (`aiofhir.capability`).
- The patient id comes only from the token response. `PatientRecord.search_raw` adds `patient=` itself and does not accept it as an argument.
- Scopes come from the server's SMART `capabilities` and `scopes_supported`. Profiles adjust discovery, search and paging. They do not choose scopes.
- An explicit `scope=` is only for an app registration that allows less than the server advertises.
- The core library does not store tokens. Tokens never appear in `repr()`, logs, or tracebacks. Patient ids in query strings are redacted in hooks.
- `verify_state()` uses `hmac.compare_digest`. Storing `state` and `code_verifier`, and using them once, is the caller's job.

## Out of scope

Writes, typed models for resources, FHIR versions other than R4, bulk data, and token storage in the core stay out unless a change asks for them.
