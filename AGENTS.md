# AGENTS.md

`aiofhir` is a thin async FHIR R4 and SMART on FHIR client. The design lives in `plans/PLAN-merged.md`. Phase 7 released 0.1.0 to PyPI. The remaining phase is Phase 8 (Epic and Cerner sandboxes), which waits for sandbox access. Add changes to `CHANGELOG.md`; a release is a GitHub release whose tag matches the version in `pyproject.toml`. Phase 8 (Epic and Cerner sandboxes) waits for sandbox access. Do not start a later phase in the same change.

When a plan item is finished, check it off in `plans/PLAN-merged.md` in the same change. Check it only when its code and tests pass.

## Commands

- Run `just check` before finishing. It syncs from `uv.lock`, then runs ruff, the format check, mypy, and pytest.
- `just test` runs pytest. Extra arguments are passed through.
- Supported Python is 3.11 through 3.13. mypy is strict.

## Library rules

- Importing `aiofhir` must not load `fastapi`. The FastAPI reference server is the optional `aiofhir[server]` extra and stays out of the core import. `RemoteRecord` uses httpx only.
- The `aiofhir[server]` backend runs beside the application's backend, never on an agent worker. It never logs tokens, subjects, `state` or callback URLs.
- The patient id comes only from the token response. Helpers add `patient=` themselves and do not accept it as an argument.
- Scopes come from the server's SMART `capabilities` and `scopes_supported`. Profiles adjust search and medication behavior. They do not choose scopes.
- An explicit `scope=` is only for an app registration that allows less than the server advertises.
- The core library does not store tokens. Tokens never appear in `repr()`, logs, or tracebacks. Patient ids in query strings are redacted in hooks.
- `verify_state()` uses `hmac.compare_digest`. Storing `state` and `code_verifier`, and using them once, is the caller's job.

## Later phases

Writes and booking, typed models for every resource, FHIR versions other than R4, bulk data, and token storage in the core stay out until their own phase.
