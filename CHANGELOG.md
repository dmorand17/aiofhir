# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Before 1.0, a minor version may
change the API.

## [Unreleased]

### Added

- `AIOFHIR_LOG_LEVEL`: set it to see aiofhir's logs with no logging code. It
  changes only the `aiofhir` logger. Records go to the app's handlers when it has
  any; while it has none, aiofhir prints them to stderr, redacted.

- `examples/streamlit_app.py` (`just demo`): a Streamlit app that signs a patient
  in, shows their Patient resource and every helper (formatted for a person and
  as `to_text()` for a model), and tries to open another patient to show aiofhir
  and the server each refusing. Streamlit comes from a new `demo` dependency
  group.

### Changed

- A failed sign-in now says why. `TokenError` names the token endpoint's status,
  `error` and `error_description` (redacted), and the token endpoint's refusals
  are logged at INFO, including the `invalid_grant` behind `AuthExpired`.
- The `aiofhir[server]` callback logs the reason for each failure: an unknown,
  used or expired state; an error from the authorization server; a missing code;
  a failed code exchange, with its cause; and a sign-in without a patient. It
  still logs no tokens, codes, `state` or subjects.

## [0.1.1] - 2026-11-27

A hardening release: fixes from the code reviews of 0.1.0. No API is removed.

### Security

- `next` links with percent-encoded dot segments (`%2e%2e`, `..%2f`) are no
  longer followed out of the FHIR base, so a server cannot steer paging, with the
  patient's token, to another path on the same host.
- `upcoming_appointments` checks the date window itself and drops appointments
  that start outside it, for servers that ignore the `date` filter.
- `FhirError` messages are redacted: server diagnostics often echo patient ids,
  identifiers or names. The raw OperationOutcome is kept on `FhirError.outcome`
  (it can hold PHI; do not log it).
- `RedactingFilter` also redacts tracebacks (`logger.exception`, `exc_info=True`)
  and `stack_info`, not only the message.
- Wider redaction in `scrub()`, `redact_text()`, `redact_url()` and
  `redact_params()`: `actor`, `performer`, `requester`, `participant`, `author`,
  `beneficiary`, `general-practitioner`, `asserter`, `recorder`, `_text` and
  `_content` values; person references under any key, URL-encoded or inside
  absolute URLs; `patient=...`-style values inside free text; `bearer` in any
  case and `Authorization: Basic` credentials; `client_secret`, `code_verifier`,
  `client_assertion` and `password` fields; token values in form bodies, query
  strings and Python dict reprs.

### Changed

- The development tools are a dependency group instead of a `dev` extra, so PyPI
  no longer lists `aiofhir[dev]`. Contributors run `uv sync --all-extras`
  (`just sync`).
- The SMART discovery cache is keyed by base URL and profile, and entries expire
  after an hour.
- `active_medications` reads referenced Medications concurrently. A failed read
  keeps the item, without a medication name, and adds a warning instead of
  failing the whole summary.
- The README security model and the getting-started logging section say what
  is redacted, how to add `RedactingFilter` to an application's own handlers,
  and that redaction is best effort.

### Fixed

- Entering an already-open `FhirClient` raises `RuntimeError` instead of leaking
  the first HTTP client.
- Discovery and token requests have a timeout (`SmartClient(timeout=10.0)`), and
  a transport failure at the token endpoint raises `TokenError`.
- Resources that a server returns twice across pages (offset paging over
  changing data) are dropped, with a warning. Offset paging can still skip a
  record that moves between pages.
- `FhirClient` retries reads and searches (GET and `POST [type]/_search`) after
  a dropped connection or a timeout, twice, with the same backoff and jitter as
  for 429/502/503/504. Each attempt reaches `on_request`.
- A response body that is not JSON (say, an HTML page from a proxy) raises
  `FhirError` from `read`, `vread`, `capabilities` and `search`, instead of a raw
  `JSONDecodeError`. In SMART discovery it counts as a failed path, so the next
  discovery path is tried and `DiscoveryError` is raised if none works.

## [0.1.0] - 2026-11-25

The first usable release. Haste Health is verified live; the Epic and Oracle
Health (Cerner) profiles are not yet verified against their sandboxes.

### Added

- SMART on FHIR sign-in: discovery with S256 PKCE, the authorization-code flow,
  `verify_state()` in constant time, and scopes derived from the server's SMART
  `capabilities` and `scopes_supported`.
- `RefreshingToken`: refreshes 60 seconds early under one lock, hands rotated
  tokens to `on_refresh`, and raises `AuthExpired` with a fixed reconnect message.
- EHR launch: `parse_ehr_launch()` and `SmartClient.for_ehr_launch()` with an
  issuer allowlist.
- `private_key_jwt` client authentication (RS384, ES384) in the `aiofhir[jwt]`
  extra, and the `client_credentials` grant for backend services.
- `FhirClient`: retries with backoff, one refresh on 401, a concurrency cap,
  paging that stays inside the FHIR base, `vread`, a cached `/metadata`, POST
  `_search` for long queries, and redacted request hooks.
- `PatientRecord` with ten helpers: upcoming appointments, active medications,
  allergies, conditions, recent labs, immunizations, vital signs, past visits,
  procedures and documents (titles and dates only). Each returns a cited
  `Summary`.
- `Record` protocol shared by `PatientRecord` and `RemoteRecord`.
- Server profiles for generic servers, Haste Health (offset paging, discovery
  fallback), Epic and Oracle Health (Cerner).
- Server-side token mode: `RemoteRecord` and the `aiofhir[server]` reference
  backend, with single-use sign-in state, a helper allowlist, and a
  Fernet-encrypted token store keyed by subject.
- Redacting log helpers: tokens, person references and patient-identifying
  search values are removed from log lines.

[Unreleased]: https://github.com/dmorand17/aiofhir/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/dmorand17/aiofhir/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/dmorand17/aiofhir/releases/tag/v0.1.0
