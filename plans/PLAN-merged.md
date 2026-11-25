# aiofhir plan

`aiofhir` is a thin **async FHIR R4 and SMART on FHIR client** for Python, built on httpx. It handles patient sign-in (`aiofhir.smart`), reading and searching, and patient-scoped helpers that return compact, cited summaries. It has **no LiveKit dependency**. LiveKit tools live in each demo repo.

- **Why async httpx:** LiveKit agents run on asyncio, so a blocking client would stall the event loop during FHIR calls and token refreshes. httpx gives pooling, timeouts, HTTP/2 and a mockable transport (respx).
- **Status:** pre-release. Phase 0 is complete. Phase 1 is in progress on `cursor/phase-1-client`: the client is not implemented yet, and the GitHub Actions run and PyPI name reservation moved here from Phase 0. Nothing has run against a live server. Check a phase item only when its code and tests pass.

## Principles
- **Patient scoping.**
  - The patient id comes only from the token response (`TokenSet.patient`). Helpers add `patient=` themselves and never accept it as an argument.
  - `search_raw()` is limited to a resource allowlist and always gets the forced patient parameter.
  - With `patient/` scopes the server binds the token to that patient's compartment, and the client check is a second layer. If the server only advertises `permission-user`, the token is user-scoped and the server's access policy is the real control.
- **Tokens server-side by default.**
  - The core library never stores tokens. Apps persist `TokenSet.to_dict()` themselves.
  - The recommended deployment is a backend that holds a `RefreshingToken` for each session. Agents get a `RemoteRecord(session_id)` that never sees a token.
  - A `PatientRecord` running in-process is for local demos and scripts only.
- **State check is the caller's job, and the docs say so.** Store `state` and `code_verifier` on the server, use them once, and compare with `verify_state()`, which uses `hmac.compare_digest`.
- **Server-agnostic.** Discovery, scopes, and client auth come from `.well-known/smart-configuration`. Search and medication quirks live in `ServerProfile`s, not in the helpers. Profiles do not choose scopes.
- **No secrets or PHI in logs.** Tokens never appear in `repr()`. Hooks get redacted URLs, so patient ids in query strings are stripped too.

## Public API
```python
from aiofhir import FhirClient, PatientRecord, RemoteRecord, AuthExpired
from aiofhir.smart import SmartClient, RefreshingToken, TokenSet, verify_state
from aiofhir.display import format_summary

smart = SmartClient(fhir_base_url, client_id, redirect_uri, scope=None, client_secret=None, profile=None)
req = await smart.authorization_request()             # discovers, builds scope from capabilities; url, state, code_verifier (S256, aud=base)
verify_state(expected=saved.state, received=params["state"])
tokens = await smart.exchange_code(code, saved.code_verifier)   # TokenSet: patient, encounter?, need_patient_banner?, fhir_user (UNVERIFIED)
rt = RefreshingToken(smart, tokens, on_refresh=save)  # asyncio.Lock; persists rotated refresh tokens

async with FhirClient(fhir_base_url, provider=rt, profile=smart.profile, max_concurrency=4) as fhir:
    rec = PatientRecord(fhir, patient_id=tokens.patient)
    s = await rec.upcoming_appointments(days=90)      # frozen Summary with citations
    print(format_summary(s, tz="America/New_York"))   # display only; never mutates s
    text = s.to_text(max_chars=4000)
    async for res in fhir.iter_search("Observation", {"patient": tokens.patient, "category": "laboratory"}):
        ...
# AuthExpired carries a fixed message: "Your health record connection expired. Please reconnect to continue."
```

## Layout
```
src/aiofhir/  __init__.py errors.py client.py record.py summary.py display.py remote.py
              smart/ (discovery.py pkce.py scopes.py client.py tokens.py)
              profiles/ (base.py generic.py haste.py epic.py cerner.py)
              server/ (optional extra [server]: reference backend for RemoteRecord)
tests/        fake_fhir.py (respx)  fixtures/{haste,epic,cerner}/  test_*.py  live/ (opt-in)
scripts/seed_haste.py   examples/smoke_haste.py
```
Extras: `[server]` (fastapi, cryptography) and, later, `[jwt]`. There is **no `[livekit]` extra**.

## Server profiles
| | Haste Health | Epic | Oracle Health (Cerner) | generic |
|---|---|---|---|---|
| Discovery | `{base}/.well-known/smart-configuration`, then fallback `/w/{t}/{p}/api/v1/.well-known/smart-configuration` | standard | standard (per-tenant base) | standard |
| Scopes | from SMART `capabilities` and `scopes_supported` (see Scopes) | same | same | same |
| Patient context | User linked to Patient via `Membership`; no picker | MyChart login | patient-persona authorize URL | token `patient` |
| Refresh | `offline_access`; rotation | **confidential client required** | `offline_access` / `online_access` | config |
| Appointment | `date=ge…&date=lt…&_sort=date` (verify) | `patient` required; no `_sort`, sort on client | **both `ge` and `lt` bounds required**; sorted ascending | as given |
| Medications | inline concept (Synthea) | `medicationReference` → read `Medication` | mostly inline (verify) | resolve references and contained resources |
| Observation | standard | expects `category` or `code` (verify); sort on client | `_count` ≤ 200; `code` ignored with `category` | standard |

## Scopes

`authorization_request()` discovers `.well-known/smart-configuration`, then `scopes_from_capabilities(cfg)` builds the authorize scope. Haste, Epic, Cerner, and any other R4 server use this one function. `READ_RESOURCES` is the helper resource set (`Patient`, `Appointment`, `MedicationRequest`, `Medication`, `AllergyIntolerance`, `Condition`, `Observation`). Phase 5 adds `Immunization`, `Encounter`, `Procedure`, and `DocumentReference` to that set.

| Piece | Rule |
|---|---|
| Context | `patient` when `permission-patient` is present. `user` only when it is absent and `permission-user` is present. |
| Permission | `.rs` when `permission-v2` is present and `scopes_supported` lists a `.rs` scope, or when v2 is present and `scopes_supported` is absent. `.read` otherwise. When both syntaxes are listed, prefer v2. |
| Resources | `{context}/*{permission}` when that wildcard is listed, or when `scopes_supported` is absent. Otherwise the intersection of `READ_RESOURCES` with `scopes_supported`. |
| Launch | `launch/patient` when it is listed, or when `launch-standalone` is present and the context is `patient`. |
| Identity | `openid` and `fhirUser` when `sso-openid-connect` is present and those scopes are listed, or when `scopes_supported` is absent. |
| Refresh | `offline_access` when `permission-offline` is present or the scope is listed. Otherwise `online_access` when it is listed. |

These are the SMART configuration fields, not the FHIR `/metadata` CapabilityStatement. An explicit `SmartClient(..., scope="...")` replaces the derived string, for an app registration that allows less than the server advertises. That is the Epic case, where the app's registered scopes are narrower than the sandbox. If `permission-patient` and `permission-user` are both missing, discovery raises `DiscoveryError`.

## Phases (each has "done when" criteria; unit tests use respx fixtures)
**Phase 0: Repo (0.5 d).**
- [x] Package named `aiofhir`, MIT license, src layout, justfile, and dev tooling.
- [x] CI workflow runs pytest, ruff, and mypy strict on Python 3.11–3.13.
- [x] `tests/test_import.py` confirms `import aiofhir` loads no `livekit` or `fastapi`. It passed locally.

**Phase 1: Build the core (2.5 d).** Left off after the failing tests. `src/aiofhir` is still only `__init__.py`.
- [x] GitHub Actions is green on this branch.
- [ ] Reserve the PyPI name `aiofhir`.
- [ ] **Discovery:** fallback paths come from the profile; S256 is required; results are cached.
  - *Tests:* Haste returns 404 on the primary path and 200 on the fallback; every path failing raises `DiscoveryError`.
- [ ] **Scopes:** `scopes_from_capabilities(cfg)` follows the Scopes table. Profiles do not override it. An explicit `scope=` replaces the derived string.
  - *Tests:* recorded configurations for Haste (`permission-v2`, `permission-patient`, `patient/*.rs`), Epic v1 (`.read` only), and Cerner (per-resource `scopes_supported`, no wildcard). Also: user-only capabilities select `user/`; both syntaxes listed prefers v2; explicit `scope=` wins; no patient or user permission raises `DiscoveryError`.
- [ ] **Auth request:** 256-bit state; verifier is 43–128 unreserved characters; `aud` is sent; `verify_state()` is constant-time.
  - *Tests:* RFC 7636 test vector; a mismatched state is refused; a reused state is refused by the reference store.
- [ ] **Exchange:** basic or post client auth is chosen from the config; a missing `patient` raises `TokenError` when the request included `launch/patient`; `TokenSet` carries `encounter` and `need_patient_banner`.
- [ ] **RefreshingToken:** refreshes 60 s early behind a lock with a double-check; persists rotation; `invalid_grant` raises `AuthExpired`.
  - *Tests:* 20 concurrent `get()` calls make exactly 1 `/token` call.
- [ ] **FhirClient:**
  - Retries 429/502/503/504 twice with backoff and jitter, honoring `Retry-After`.
  - On a 401, refreshes once and retries once, then raises `AuthExpired`.
  - Drops OperationOutcome entries into `.warnings`.
  - Follows only same-origin `next` links, with `max_pages=10`.
  - Configurable timeouts, a concurrency semaphore (default 4), and `iter_search`.
- [ ] **PatientRecord:** forced patient and allowlist. `upcoming_appointments` uses `date=ge{now}&date=lt{now+days}&_sort=date`, then a client-side booked/pending filter and sort.
  - *Tests:* passing `patient` raises `TypeError`; `search_raw("Account")` is refused; cancelled and proposed appointments are dropped.
- [ ] **Summary:** frozen; `to_text(max_chars)` cuts on item boundaries with "(+N more)" and keeps `Type/id` citations; FHIR free text is marked as data; `display.format_summary` is pure.

**Phase 2: Profiles (1 d).** Scope selection stays in `scopes_from_capabilities`.
- [ ] `generic`, `haste`, and `epic` are filled in, and `cerner` starts as a stub, using `adapt_search(resource, params)`, `resolve_medication`, and `discovery_paths`.
- [ ] Switching servers is a one-line change and the helpers have no vendor branches.
- *Tests:* each profile's `adapt_search`, for example Cerner adds an `lt` bound and Epic drops `_sort`.

**Phase 3: Live against Haste (1.5 d).** Run local Haste with docker-compose: API on :3000, admin on :3001, tenant `my-health`, admin `myuser@health.org` / `testing_password`. Create the project `lk-demo`.
- [ ] **`scripts/seed_haste.py`** is idempotent and creates:
  - a Patient with Practitioners and Locations;
  - Appointments at past, +3/+14/+45 days, one pending and one cancelled;
  - active and stopped MedicationRequests, covering inline codes, references and contained Medications;
  - an AllergyIntolerance, Conditions and lab Observations;
  - a User with a `Membership` linked to the Patient, plus a **read-only, own-patient AccessPolicyV2**;
  - a `ClientApplication` with redirect `http://localhost:8765/callback`, grants `authorization_code` and `refresh_token`, and an allowed scope copied from that server's `scopes_supported` (so the derived request is a subset of what the app may ask for).
- [ ] **`examples/smoke_haste.py`:** browser login with no `scope=` argument, state check, code exchange, every helper printed, one forced refresh. The requested scope is the one `scopes_from_capabilities` built.
- [ ] **`tests/live`:** opt-in through environment credentials. It checks that the token has a `patient`, that every resource returned belongs to that patient, and that paging terminates.
- [ ] **Recorded, de-identified fixtures** go into the unit tests, with a scrubber check.
- *Done when:* the live tests pass, the cancelled appointment is filtered out, every item has a citation, and no token appears in any output.

**Phase 4: Server-side token mode (1 d).** `RemoteRecord` mirrors the `PatientRecord` methods. The optional `aiofhir[server]` reference backend has:
- [ ] `/auth/start` and `/auth/callback` with single-use state.
- [ ] `/v1/record/{method}` with a service bearer and a method allowlist.
- [ ] A Fernet-encrypted store.
- *Tests:* end to end over ASGI; an unknown session raises `AuthExpired`; no token appears in logs (caplog).

**Phase 5: More helpers (1 d).**
- [ ] `immunizations`, `vital_signs`, `past_visits` (Encounter), `procedures`, and `documents` (titles and dates only), all in one summary style.

**Phase 6: Epic and Cerner sandboxes (1.5 d).** Run every helper against the Epic and Cerner sandboxes and record the differences in their profiles.
- [ ] Epic sandbox. Test users: `fhircamila`, `fhirderrick`, and `fhirdesiree`. They have no appointment data.
- [ ] Cerner sandbox. Patient endpoint: `fhir-myrecord.cerner.com/r4/ec2458f2-…`.
- *Done when:* the live tests pass on both, with skips documented.

**Phase 7: Auth extensions, later (1.5 d).**
- [ ] `private_key_jwt` with RS384/ES384 under `aiofhir[jwt]`, which Epic production prefers.
- [ ] `client_credentials` for seeding and CI.
- [ ] EHR launch: parse `iss` and `launch`, then discover from `iss`.
- [ ] POST `_search` (form-encoded), `vread`, and a cached FHIR `/metadata` CapabilityStatement. SMART `capabilities` are already read during discovery.
- [ ] Request hooks with PHI redaction.

**Phase 8: Release (0.5 d).**
- [ ] Docs (getting started, sign-in and state handling, profiles, helper reference), semver, a changelog, and PyPI trusted publishing.

**Effort:** about 5.5 days to reach live against Haste (Phases 0–3), about 8.5 days through Epic and Cerner (Phases 0–6), and about 10.5 days for everything.

## Out of scope
- Writes and booking.
- Typed models for every resource.
- FHIR versions other than R4.
- Bulk data.
- Token storage in the core (apps persist `TokenSet`; the optional server extra is a reference only).
- LiveKit tools and UI.

## Open questions
- **Haste:** the base URL, hosted (BAA, rate limits) or self-hosted, and the tenant and project. Can I create Users, Memberships, AccessPolicies and the ClientApplication? Does it support `_sort` and two date bounds? Phase 3 records the live `capabilities` document and the scope string that authorize accepted.
- **Repo:** `dmorand17/aiofhir` or the LiveKit-Field org? License is MIT.
- **Scope:** keep aiofhir focused on patient reads for agents rather than a general FHIR client.
- **Server extra:** keep the reference backend in this repo, or move it to `examples/`?

## Next: integrations
The Agents 2.0 chat demo and its LiveKit tools, built on `RemoteRecord`, get their own plan in the demo repo once Phase 3 passes.
