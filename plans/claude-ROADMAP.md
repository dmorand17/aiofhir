# aiofhir roadmap

`aiofhir` is an async FHIR R4 client for Python. It handles SMART on FHIR sign-in, reading and searching, and patient-scoped helpers that return compact summaries for LLM agents. It started as `thinfhir`, built for LiveKit agent demos against Haste Health and Epic.

**Goal:** one library that any demo can install to sign a patient in and answer questions like "when is my next appointment?" or "what medications am I taking?", against Haste Health today and Epic later, by changing configuration only.

**Status:** pre-release. Sign-in (standalone launch), read and search with paging, token refresh, retries and five patient helpers exist, with unit tests against a mocked server. Nothing has run against a live server yet.

---

## Phase 0: Rename and set up the repo

- [ ] Rename the package `thinfhir` → `aiofhir` (`src/aiofhir`, imports, `pyproject.toml`, README)
- [ ] Add a license
- [ ] CI with GitHub Actions: `pytest`, `ruff`, `mypy` on Python 3.10–3.13
- [ ] Optional: publish a placeholder `0.0.1` to PyPI to reserve the name

**Done when:** CI is green on `main` and `pip install -e .` gives `import aiofhir`.

## Phase 1: A complete async read client

- [ ] `vread(type, id, version)`
- [ ] Search by POST (`_search`) for long queries
- [ ] `capabilities()`: read and cache `/metadata`
- [ ] `iter_search(...)`: an async iterator over all pages, so large results stream
- [ ] A limit on concurrent requests (default 4), so parallel tool calls don't trip rate limits
- [ ] Configurable timeouts and retry policy
- [ ] Request and response hooks for logging and auditing, with PHI stripped by default

**Done when:** every method has unit tests, and the README shows `async for` paging.

## Phase 2: Authentication

- [ ] **EHR launch:** parse `iss` and `launch` from the launch URL and discover endpoints from `iss`
- [ ] **Private-key client authentication** (`private_key_jwt`, RS384 / ES384) for confidential clients
- [ ] **Backend services** (`client_credentials`) for seeding data and running tests
- [ ] Put JWT signing behind an optional install: `aiofhir[jwt]`
- [ ] Return the `encounter` and `need_patient_banner` launch context in `TokenSet`

**Done when:** both launch modes and all three client authentication types work against Haste Health.

## Phase 3: Server profiles

- [ ] A `ServerProfile` that records each server's scope format and search quirks, so they don't end up as special cases throughout the code
- [ ] Profiles: `generic`, `haste`, `epic`
- [ ] Haste profile, from Haste's docs:
  - [ ] Scopes: try `patient/*.rs`; fall back to `user/*.rs` + `launch/patient` (the docs disagree about whether `patient/` scopes are accepted)
  - [ ] Standalone launch has no patient picker: the patient comes from the signed-in user's `Membership` link
  - [ ] FHIR base URL: `https://<host>/w/<tenant>/<project>/api/v1/fhir`
- [ ] Epic profile: filled in during the Epic sandbox pass (Phase 5)

**Done when:** switching servers is a one-line profile change and the helpers need no server-specific code.

## Phase 4: More patient helpers

Existing: `upcoming_appointments`, `active_medications`, `allergies`, `conditions`, `recent_labs`.

- [ ] `immunizations()`
- [ ] `past_visits()` (Encounter)
- [ ] `procedures()`
- [ ] `vital_signs()` (Observation, `category=vital-signs`)
- [ ] `documents()` (DocumentReference, titles and dates only)
- [ ] One consistent summary style across all helpers
- [ ] Time zone handling everywhere a date is shown

**Done when:** each helper has unit tests and returns useful data from the seeded Haste patient.

## Phase 5: Testing against real servers

- [ ] **Haste seed script** that creates:
  - a test Patient with Practitioners and Locations
  - past, upcoming and cancelled Appointments
  - active and stopped MedicationRequests (inline codes, references and contained Medications)
  - AllergyIntolerance, Condition and lab Observations
  - a user linked to the Patient through `Membership`
  - a `ClientApplication` with redirect URI, grants and scopes
- [ ] **Live tests** that run only when credentials are set: sign in, check the token has `patient`, and call each helper
- [ ] **Epic sandbox pass:** run every helper against Epic's test patients and record the differences in the Epic profile
- [ ] **Saved fixtures:** de-identified responses from both servers, used in the regular unit tests

**Done when:** live tests pass against Haste Health and the Epic sandbox.

## Phase 6: LiveKit and release

- [ ] Optional `aiofhir[livekit]` add-on with ready-made function tools
- [ ] `examples/livekit_text_agent/`: a complete text-chat agent with sign-in
- [ ] Docs: getting started, sign-in flow, server profiles, helper reference
- [ ] Semantic versioning and a changelog
- [ ] Publish to PyPI from GitHub Actions (trusted publishing)

**Done when:** `pip install aiofhir[livekit]` and the example agent run end to end against Haste Health.

---

## Out of scope

- Writing data (create, update, delete)
- Typed models for every FHIR resource
- Storing tokens: each app stores its own `TokenSet` (`to_dict` / `from_dict` are provided)
- FHIR versions other than R4

## Open questions

- **How general should aiofhir be?** This roadmap keeps it focused on reading patient data for agents. A general-purpose async FHIR client would bring writes and typed models into scope, which is a much bigger project.
- **License:** MIT or Apache-2.0?
- **Does Haste Health accept `patient/` scopes?** Settle it in Phase 5 and update the Haste profile.
