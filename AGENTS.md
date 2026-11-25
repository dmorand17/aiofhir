# AGENTS.md

`aiofhir` is a thin async FHIR R4 and SMART on FHIR client. The design lives in `plans/PLAN-merged.md`. Phase 0 is the repo scaffold. The next implementation phase is Phase 1: discovery, scopes, sign-in, the FHIR client, patient helpers, and summaries. Do not start a later phase in the same change.

When a plan item is finished, check it off in `plans/PLAN-merged.md` in the same change. Check it only when its code and tests pass.

## Commands

- Run `just check` before finishing. It syncs from `uv.lock`, then runs ruff, the format check, mypy, and pytest.
- `just test` runs pytest. Extra arguments are passed through.
- Supported Python is 3.11 through 3.13. mypy is strict.

## Library rules

- Importing `aiofhir` must not load `fastapi`. The FastAPI reference server is a later optional extra and stays out of the core import.
- The patient id comes only from the token response. Helpers add `patient=` themselves and do not accept it as an argument.
- Scopes come from the server's SMART `capabilities` and `scopes_supported`. Profiles adjust search and medication behavior. They do not choose scopes.
- An explicit `scope=` is only for an app registration that allows less than the server advertises.
- The core library does not store tokens. Tokens never appear in `repr()`, logs, or tracebacks. Patient ids in query strings are redacted in hooks.
- `verify_state()` uses `hmac.compare_digest`. Storing `state` and `code_verifier`, and using them once, is the caller's job.

## Later phases

Writes and booking, typed models for every resource, FHIR versions other than R4, bulk data, and token storage in the core stay out until their own phase.
