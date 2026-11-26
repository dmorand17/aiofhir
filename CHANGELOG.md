# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Before 1.0, a minor version may
change the API.

## [Unreleased]

### Security

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

### Fixed

- `FhirClient` retries reads and searches (GET and `POST [type]/_search`) after
  a dropped connection or a timeout, twice, with the same backoff and jitter as
  for 429/502/503/504. Each attempt reaches `on_request`.

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

[0.1.0]: https://github.com/dmorand17/aiofhir/releases/tag/v0.1.0
